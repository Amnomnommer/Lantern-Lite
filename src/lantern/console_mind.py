"""The console (owner amendment, 2026-10-24): a small neural network each Lantern trains for itself.

Owner: "We could treat the five core minds that we have as the governors, like the inside emotions from the
Pixar movie Inside Out and have the neural network shift through interactions and working with them."
"I like each Lantern controlling its own, that would develop the personality better."

- **The five minds are the characters at the console.** The fact mind always speaks first, with what
  was read. Association, feeling, sleep and expectation each propose something to add:
  - association: "it reminds me of…";
  - feeling: how the idea feels to writers;
  - sleep: something worked out;
  - expectation: a curious question about what usually comes next.
- **The console learns which voices to listen to, and when.** It is a small neural network (12
  inputs, 8 hidden units, one output per voice). It learns from what happened next:
  - the owner's ratings and reactions;
  - ideas that come back (owner, 2026-10-24: "learn from more than that"). Every choice is logged
    with the ideas each voice offered, heard or not. When one returns within two days, that voice
    offered something that mattered: in the owner's next words (strongest), in the other
    Lantern's turns in a talk, or in what its own reading learned next;
  - a night's replay: at the morning sleep it goes over the day's lessons once more, gently.
- **Temperament first.** It starts from the Lantern's own temperament (a curious Lantern leans
  towards expectation, a warm one towards feeling), so two Lanterns grow apart as they live.
- **It only chooses.** It never writes text and never touches memory, goals, permissions, identity
  or safety rules ("governor" stays the name for those).
- **Deterministic and inspectable.** Exploration uses seeded randomness. Every choice records its
  reasons (temperament against what was learned).
- **Under the owner's control.** Each day's weights are kept as a version: `!console`,
  `!console off|on`, `!console restore N`.

It is this Lantern's own personality state and is never shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import re

VOICES = ("associations", "feeling", "sleep", "expectation")  # the fact mind always speaks first
FEATURES = ("bias", "question", "length", "owner_warmth", "owner_surprise", "known", "evening", "strength")
INPUTS = len(FEATURES) + len(VOICES)
HIDDEN = 8
MAX_VOICES = 2
LEARNING_RATE = 0.15
MAX_VERSIONS = 30
MAX_LOG = 120  # recent choices kept for ideas that come back
MAX_LESSONS = 400  # lessons kept for the night's replay
COME_BACK_DAYS = 2
# How much a returning idea teaches, by where it came back.
SOURCES = {"you": 0.6, "talk": 0.35, "reading": 0.4}
REPLAY_WEIGHT = 0.4
# Which trait each voice answers to (the Personality Core's traits, levels 1 to 5).
TEMPERAMENT = {"associations": ("playfulness", "humor"), "feeling": ("warmth",), "sleep": ("explanation_detail",),
               "expectation": ("curiosity",)}


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def prior(levels: dict[str, int] | None, voice: str) -> float:
    """The temperament's lean towards a voice, as a logit: level 3 is neutral."""
    traits = TEMPERAMENT[voice]
    levels = levels or {}
    return round(sum(float(levels.get(t, 3)) - 3.0 for t in traits) / len(traits) * 0.6, 4)


@dataclass
class Decision:
    """One choice at the console: what was offered, what was chosen, and why."""

    context: list[float]
    offered: dict[str, dict[str, object]]  # voice -> {"strength", "ideas", "p", "temperament", "learned"}
    chosen: list[str]
    explored: bool = False
    rewarded: set[str] = field(default_factory=set)


class Console:
    def __init__(self, path: Path | None, *, seed: str, levels: dict[str, int] | None = None) -> None:
        self.path = None if path is None else Path(path)
        self.seed = seed
        self.levels = levels or {}
        state = None
        if self.path is not None and self.path.is_file():
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = None
        if state is None:
            rng = random.Random(f"console:{seed}")
            state = {"format": "lantern_console_v1", "enabled": True, "lessons": 0, "decisions": 0,
                     "w1": [[round(rng.uniform(-0.3, 0.3), 6) for _ in range(INPUTS)] for _ in range(HIDDEN)],
                     "b1": [0.0] * HIDDEN, "w2": [round(rng.uniform(-0.3, 0.3), 6) for _ in range(HIDDEN)], "b2": 0.0,
                     "versions": {}, "recent": []}
        self.state = state

    # -- the network ---------------------------------------------------------------------

    def _input(self, context: list[float], voice: str) -> list[float]:
        return list(context) + [1.0 if v == voice else 0.0 for v in VOICES]

    def _forward(self, x: list[float]) -> tuple[list[float], float]:
        hidden = [math.tanh(sum(w * xi for w, xi in zip(row, x)) + b) for row, b in zip(self.state["w1"], self.state["b1"])]
        return hidden, sum(w * h for w, h in zip(self.state["w2"], hidden)) + self.state["b2"]

    def score(self, context: list[float], voice: str) -> dict[str, float]:
        """How much this Lantern wants to hear a voice here: temperament plus what it learned."""
        _, learned = self._forward(self._input(context, voice))
        lean = prior(self.levels, voice)
        return {"p": round(_sigmoid(lean + learned), 4), "temperament": lean, "learned": round(learned, 4)}

    def _train(self, context: list[float], voice: str, target: float, weight: float) -> None:
        x = self._input(context, voice)
        hidden, learned = self._forward(x)
        p = _sigmoid(prior(self.levels, voice) + learned)
        delta = (p - target) * weight * LEARNING_RATE  # cross-entropy gradient at the output
        w2 = self.state["w2"]
        for j, h in enumerate(hidden):
            grad_hidden = delta * w2[j] * (1 - h * h)
            w2[j] -= delta * h
            self.state["b1"][j] -= grad_hidden
            row = self.state["w1"][j]
            for i, xi in enumerate(x):
                row[i] -= grad_hidden * xi
        self.state["b2"] -= delta

    # -- choosing and learning -----------------------------------------------------------

    def enabled(self) -> bool:
        return bool(self.state.get("enabled", True))

    def choose(self, context: list[float], offered: dict[str, dict[str, object]]) -> Decision:
        """Which voices join the reply (at most two); the fixed rule when the owner turned the console off."""
        voices = [v for v in VOICES if v in offered]
        if not self.enabled():
            return Decision(context, {v: dict(offered[v]) for v in voices}, voices[:MAX_VOICES])
        scored = {}
        for voice in voices:
            features = list(context)
            features[FEATURES.index("strength")] = float(offered[voice].get("strength", 0.5))
            scored[voice] = {**offered[voice], **self.score(features, voice), "features": features}
        ranked = sorted(voices, key=lambda v: (-scored[v]["p"], VOICES.index(v)))
        chosen = [v for v in ranked if scored[v]["p"] >= 0.5][:MAX_VOICES]
        self.state["decisions"] = int(self.state.get("decisions", 0)) + 1
        rng = random.Random(f"explore:{self.seed}:{self.state['decisions']}")
        explore = max(0.05, 0.3 * 0.97 ** int(self.state.get("lessons", 0)))
        explored = False
        if voices and rng.random() < explore:  # now and then, try a voice it would not have chosen (or drop one)
            pick = rng.choice(voices)
            chosen = [v for v in chosen if v != pick] if pick in chosen else (chosen + [pick])[-MAX_VOICES:]
            explored = True
        decision = Decision(context, scored, chosen, explored)
        self.state["log"] = (self.state.get("log", []) + [{
            "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "chosen": chosen,
            "voices": {v: {"ideas": [str(i) for i in scored[v].get("ideas", [])][:4], "features": scored[v]["features"]}
                       for v in voices}, "credited": {}}])[-MAX_LOG:]
        self.state["recent"] = (self.state.get("recent", []) + [{
            "chosen": chosen, "explored": explored,
            "offered": {v: {"p": scored[v]["p"], "temperament": scored[v]["temperament"], "learned": scored[v]["learned"]}
                        for v in voices}}])[-20:]
        self.save()
        return decision

    def reward(self, decision: Decision | None, good: bool, *, voices: list[str] | None = None, weight: float = 1.0) -> int:
        """What happened next teaches the console: the chosen voices (or the given ones) were good or not."""
        if decision is None or not self.enabled():
            return 0
        taught = 0
        if voices is None:
            # A rating of the whole reply: the voices heard get it in full; the ones left out learn the
            # opposite, lightly (a good reply without them says leaving them out was right).
            lessons = [(v, good, weight) for v in decision.chosen] + [
                (v, not good, weight * 0.3) for v in decision.offered if v not in decision.chosen]
        else:
            lessons = [(v, good, weight) for v in voices]
        for voice, target, strength in lessons:
            if voice in decision.rewarded or voice not in decision.offered:
                continue
            self._learn(decision.offered[voice]["features"], voice, 1.0 if target else 0.0, strength, "you")
            decision.rewarded.add(voice)
            taught += 1
        if taught:
            self.save()
        return taught

    def _learn(self, features: list[float], voice: str, target: float, weight: float, source: str) -> None:
        """One lesson: trained now, kept for the night's replay, counted by where it came from."""
        self._train(features, voice, target, weight)
        self.state["lessons"] = int(self.state.get("lessons", 0)) + 1
        sources = self.state.setdefault("sources", {})
        sources[source] = int(sources.get(source, 0)) + 1
        self.state["buffer"] = (self.state.get("buffer", []) + [
            {"features": features, "voice": voice, "target": target, "weight": weight}])[-MAX_LESSONS:]

    def notice(self, text: str, source: str, *, now: datetime | None = None) -> int:
        """Ideas that come back: a voice whose idea appears again (in the owner's words, the other Lantern's
        turn or its own new reading) offered something that mattered, whether or not it was heard."""
        if not self.enabled() or source not in SOURCES or not text:
            return 0
        lowered = f" {text.lower()} "
        now = now or datetime.now(timezone.utc)
        taught = 0
        for entry in reversed(self.state.get("log", [])):
            try:
                age = (now - datetime.fromisoformat(str(entry["at"]))).total_seconds()
            except (KeyError, ValueError):
                continue
            if age > COME_BACK_DAYS * 86400:
                break
            credited = entry.setdefault("credited", {})
            for voice, offer in entry.get("voices", {}).items():
                if source in credited.get(voice, []) or voice not in VOICES:
                    continue
                if any(len(i) > 2 and re.search(rf"(?<![a-z]){re.escape(i.lower())}(?:s|es)?(?![a-z])", lowered)
                       for i in offer.get("ideas", [])):
                    # Heard or not: a voice that brought in an idea which came back gets a lesson either way.
                    self._learn(offer["features"], voice, 1.0, SOURCES[source], source)
                    credited[voice] = [*credited.get(voice, []), source]
                    taught += 1
        if taught:
            self.save()
        return taught

    def replay(self) -> int:
        """At the morning sleep: the day's lessons once more, gently (consolidation, as sleep does for memory)."""
        if not self.enabled():
            return 0
        buffer = self.state.get("buffer", [])
        for lesson in buffer:
            self._train(lesson["features"], lesson["voice"], float(lesson["target"]), float(lesson["weight"]) * REPLAY_WEIGHT)
        self.state["buffer"] = []
        self.state["replayed"] = int(self.state.get("replayed", 0)) + len(buffer)
        if buffer:
            self.save()
        return len(buffer)

    # -- the owner's controls --------------------------------------------------------------

    def save(self) -> None:
        if self.path is None:
            return
        day = datetime.now(timezone.utc).date().isoformat()
        versions = self.state.setdefault("versions", {})
        versions[day] = {k: self.state[k] for k in ("w1", "b1", "w2", "b2", "lessons")}
        for old in sorted(versions)[:-MAX_VERSIONS]:
            versions.pop(old)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")
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
        kept = json.loads(json.dumps(self.state["versions"][days[number - 1]]))
        self.state.update(kept)
        self.save()
        return days[number - 1]

    def explain(self) -> str:
        recent = self.state.get("recent", [])
        sources = self.state.get("sources", {})
        where = ", ".join(f"{n} from {'you' if s == 'you' else 'our talks' if s == 'talk' else 'my reading'}"
                          for s, n in sorted(sources.items()) if n)
        lines = [f"My console ({'on' if self.enabled() else 'off: fixed rules'}; {self.state.get('lessons', 0)} lesson(s)"
                 + (f": {where}" if where else "") + "):"]
        for voice in VOICES:
            ps = [r["offered"][voice]["p"] for r in recent if voice in r.get("offered", {})]
            listening = f"{round(sum(ps) / len(ps) * 100)}% lately" if ps else "not heard yet"
            lean = prior(self.levels, voice)
            mood = "my temperament leans towards it" if lean > 0 else "my temperament leans away" if lean < 0 else "neutral by temperament"
            lines.append(f"• {voice}: {listening} ({mood})")
        if recent:
            last = recent[-1]
            lines.append("Last choice: " + (", ".join(last["chosen"]) or "just the facts")
                         + (" (trying something new)" if last["explored"] else ""))
        days = self.versions()
        if days:
            lines.append(f"Versions: {', '.join(f'{i}. {d}' for i, d in enumerate(days, 1))}. "
                         "!console restore N goes back; !console off uses fixed rules.")
        return "\n".join(lines)
