"""Replay and audit the phone's Gate 7 learning ledger on Prime (W-M1 backwards merge).

The phone learns through an append-only, hash-chained ledger
(`lantern_phone_learning_v1`, `PhoneLearning.kt`): ratings, trials, evaluations,
adoptions and reverts. Every phone view is recomputed by replaying it. This
module replays the same ledger in Python so Prime can check, before anything
becomes authoritative, that every decision the phone recorded follows from the
evidence before it:

- the chain is complete from its first event and every digest matches;
- every evaluation's data equals a fresh evaluation of the ledger up to it;
- every strategy adoption cites the strategy's latest evaluation, which retained it;
- every personality adoption matches a proposal recomputed from the voice ratings;
- every revert undoes something that was in force.

It only reads. Its report is for owner review; nothing here writes Prime's memory.
Integers only (millis), so results match the phone byte for byte.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from lantern.personality_runtime import VOICE_RULES
from lantern.phone_experience import validate_learning

FORMAT = "lantern_phone_learning_v1"
STARTING_CONFIDENCE = 500
MIN_CONTEXTS = 2
MAX_CONTEXT_GAP = 350
MAX_DELTA = 150
PERSONALITY_MIN_RATINGS = 3
# Identical to PhoneLearning.STRATEGIES: id -> the reply act it reshapes.
STRATEGIES = {
    "concept_link": "reflective_acknowledgement",
    "concept_recall": "grounded_question",
    "phrasebook": "reflective_acknowledgement",
    "reading_link": "reflective_acknowledgement",
}
RULE_TRAITS = {rule.rule_id: rule.trait for rule in VOICE_RULES}


@dataclass(frozen=True, slots=True)
class Rating:
    digest: str
    sequence: int
    turn: str
    about: str
    act: str
    strategy: str
    context: str
    score: int
    rules: tuple[str, ...]


@dataclass(slots=True)
class LearningView:
    trials: set[str] = field(default_factory=set)
    adopted: set[str] = field(default_factory=set)
    adopted_concepts: set[str] = field(default_factory=set)
    rejected_concepts: set[str] = field(default_factory=set)
    ratings: list[Rating] = field(default_factory=list)
    evaluations: dict[str, dict[str, object]] = field(default_factory=dict)
    confidence: dict[str, int] = field(default_factory=dict)
    personality_adoptions: dict[str, dict[str, object]] = field(default_factory=dict)
    personality_since: dict[str, int] = field(default_factory=dict)
    facts: dict[str, str] = field(default_factory=dict)


def view(ledger: Sequence[Mapping[str, object]]) -> LearningView:
    """Port of `PhoneLearning.view`."""
    v = LearningView()
    for event in ledger:
        d = event["data"]
        assert isinstance(d, dict)
        kind = event["kind"]
        if kind == "rating":
            v.ratings.append(Rating(
                str(event["digest"]), int(event["sequence"]), d["turn"], d["about"], d["act"],
                d["strategy"], d["context"], int(d["score"]), tuple(d["rules"]),
            ))
        elif kind == "trial":
            v.trials.add(d["id"])
        elif kind == "stop":
            v.trials.discard(d["id"])
        elif kind == "evaluation":
            v.evaluations[d["strategy"]] = dict(event)
            v.confidence[d["strategy"]] = int(d["confidence_after"])
        elif kind in ("adopt", "revert", "reject"):
            _target(v, event)
    return v


def _target(v: LearningView, event: Mapping[str, object]) -> None:
    d = event["data"]
    assert isinstance(d, dict)
    kind = event["kind"]
    target = d.get("target")
    if target == "strategy":
        if kind == "adopt":
            v.adopted.add(d["id"])
            v.trials.discard(d["id"])
        else:
            v.adopted.discard(d["id"])
    elif target == "fact":
        if kind == "adopt":
            v.facts[d["reference"]] = d["text"]
        else:
            v.facts.pop(d["reference"], None)
    elif target == "concept":
        ref = d["reference"]
        if kind == "adopt":
            v.adopted_concepts.add(ref)
            v.rejected_concepts.discard(ref)
        elif kind == "reject":
            v.rejected_concepts.add(ref)
            v.adopted_concepts.discard(ref)
        else:
            v.adopted_concepts.discard(ref)
            v.rejected_concepts.discard(ref)
    elif target == "personality":
        trait = d["trait"]
        v.personality_since[trait] = int(event["sequence"])
        if kind == "adopt":
            v.personality_adoptions[trait] = dict(event)
        else:
            v.personality_adoptions.pop(trait, None)


def evaluation_data(ledger: Sequence[Mapping[str, object]], strategy: str) -> dict[str, object] | None:
    """The data `PhoneLearning.evaluate` would record now, or None when there is too little evidence."""
    act = STRATEGIES[strategy]
    v = view(ledger)
    relevant = [r for r in v.ratings if r.about == "reply" and r.act == act and r.strategy in ("baseline", strategy)]
    trials: list[dict[str, object]] = []
    for context in sorted({r.context for r in relevant}):
        base = [r for r in relevant if r.context == context and r.strategy == "baseline"]
        cand = [r for r in relevant if r.context == context and r.strategy == strategy]
        if base and cand:
            trials.append({
                "baseline_millis": sum(r.score for r in base) * 1000 // len(base),
                "baseline_ratings": len(base),
                "candidate_millis": sum(r.score for r in cand) * 1000 // len(cand),
                "candidate_ratings": len(cand),
                "context": context,
            })
    if not trials:
        return None
    improvements = [int(t["candidate_millis"]) - int(t["baseline_millis"]) for t in trials]
    issues: list[str] = []
    if len(trials) < MIN_CONTEXTS:
        issues.append("benchmark_overfit_risk")
    if max(improvements) - min(improvements) > MAX_CONTEXT_GAP:
        issues.append("brittle_context_gap")
    mean = sum(improvements) // len(improvements)
    retain = mean > 0 and not issues
    candidate = [r for r in relevant if r.strategy == strategy]
    before = v.confidence.get(strategy, STARTING_CONFIDENCE)
    errors = sum(r.score * 1000 - before for r in candidate)
    delta = max(-MAX_DELTA, min(MAX_DELTA, errors // (5 * len(candidate))))
    after = max(0, min(999, before + delta))
    return {
        "confidence_after": after, "confidence_before": before,
        "contexts": [t["context"] for t in trials],
        "counterevidence": [r.digest for r in candidate if r.score == 0],
        "decision": "retain_candidate" if retain else "reverse_candidate",
        "evidence": [r.digest for r in relevant], "issues": issues, "mean_improvement_millis": mean,
        "strategy": strategy, "trials": trials,
    }


def personality_proposal(ledger: Sequence[Mapping[str, object]], trait: str, level: int) -> dict[str, object] | None:
    """Port of `PhoneLearning.personalityProposals` for one trait at `level`."""
    v = view(ledger)
    since = v.personality_since.get(trait, -1)
    rated = [r for r in v.ratings if r.about == "voice" and r.sequence > since
             and any(RULE_TRAITS.get(rule) == trait for rule in r.rules)]
    if len(rated) < PERSONALITY_MIN_RATINGS:
        return None
    mean = sum(r.score for r in rated) * 1000 // len(rated)
    target = level + 1 if mean >= 750 else level - 1 if mean <= 250 else level
    target = max(0, min(5, target))
    if target == level:
        return None
    return {"after": target, "before": level, "evidence": [r.digest for r in rated], "mean_millis": mean}


def audit(ledger: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Replay a whole phone ledger and list every decision that does not follow from its evidence."""
    events = validate_learning(list(ledger)) if len(ledger) <= 5000 else None
    if events is None:
        raise ValueError("learning ledger is longer than the phone allows")
    problems: list[str] = []
    if events and (events[0]["sequence"] != 0 or events[0]["previous"] is not None):
        problems.append("ledger does not start at its first event")
    for index, event in enumerate(events):
        prefix = events[:index]
        before = view(prefix)
        d = event["data"]
        where = f"event {event['sequence']} ({event['kind']})"
        target = d.get("target")
        if event["kind"] == "rating":
            if d.get("strategy") not in ("baseline", *STRATEGIES) or d.get("score") not in (0, 1):
                problems.append(f"{where}: unknown strategy or score")
        elif event["kind"] == "trial":
            if d.get("id") not in STRATEGIES or d["id"] in before.trials or d["id"] in before.adopted:
                problems.append(f"{where}: trial of an unknown, running or adopted strategy")
        elif event["kind"] == "stop":
            if d.get("id") not in before.trials:
                problems.append(f"{where}: stops a strategy that was not in trial")
        elif event["kind"] == "evaluation":
            strategy = d.get("strategy")
            expected = evaluation_data(prefix, strategy) if strategy in STRATEGIES else None
            if expected != d:
                problems.append(f"{where}: does not match a fresh evaluation of the evidence before it")
        elif target == "strategy":
            if event["kind"] == "adopt":
                latest = before.evaluations.get(d.get("id"))
                if (latest is None or latest["digest"] != d.get("evaluation")
                        or latest["data"]["decision"] != "retain_candidate"):
                    problems.append(f"{where}: adopts {d.get('id')} without its latest evaluation retaining it")
            elif d.get("id") not in before.adopted:
                problems.append(f"{where}: reverts {d.get('id')}, which was not adopted")
        elif target == "personality":
            trait = d.get("trait")
            if event["kind"] == "adopt":
                expected = personality_proposal(prefix, trait, int(d.get("before", -1)))
                actual = {k: d.get(k) for k in ("after", "before", "evidence", "mean_millis")}
                if expected != actual:
                    problems.append(f"{where}: personality change for {trait} does not match its voice ratings")
            else:
                adoption = before.personality_adoptions.get(trait)
                if adoption is None or adoption["digest"] != d.get("adoption") or adoption["data"]["before"] != d.get("after"):
                    problems.append(f"{where}: personality revert for {trait} does not undo the change in force")
        elif target == "fact" and event["kind"] == "revert" and d.get("reference") not in before.facts:
            problems.append(f"{where}: forgets a fact that was not remembered")
    final = view(events)
    return {
        "events": len(events),
        "problems": problems,
        "consistent": not problems,
        "adopted_strategies": sorted(final.adopted),
        "strategies_in_trial": sorted(final.trials),
        "confidence_millis": dict(sorted(final.confidence.items())),
        "personality_changes": {t: e["data"]["after"] for t, e in sorted(final.personality_adoptions.items())},
        "known_concepts": sorted(final.adopted_concepts),
        "remembered_facts": len(final.facts),
        "ratings": len(final.ratings),
        "review_only": True,
    }


def _events_in(path: Path) -> list[dict[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    events = value if isinstance(value, list) else value.get("ledger", value.get("learning", []))
    if not isinstance(events, list):
        raise ValueError(f"{path} holds no learning events")
    return events


def assemble(paths: Sequence[Path]) -> list[dict[str, object]]:
    """Join ledger slices (one per shared packet) into one ledger, in sequence order.

    The same event may arrive twice (a re-share); different events claiming the
    same sequence number are refused rather than chosen between.
    """
    by_sequence: dict[int, dict[str, object]] = {}
    for path in paths:
        for event in _events_in(path):
            sequence = event.get("sequence")
            if not isinstance(sequence, int):
                raise ValueError(f"{path}: learning event without a sequence")
            known = by_sequence.setdefault(sequence, event)
            if known.get("digest") != event.get("digest"):
                raise ValueError(f"two different learning events claim sequence {sequence}")
    return [by_sequence[s] for s in sorted(by_sequence)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lantern.learning.phone_ledger",
        description=(
            "Audit the phone's learning ledger. PATH is a JSON ledger, a filed experience package, "
            "or a directory of them (for example the Commons experience/ folder); slices are joined in order."
        ),
    )
    parser.add_argument("path")
    args = parser.parse_args(argv)
    path = Path(args.path)
    paths = sorted(path.glob("*.json")) if path.is_dir() else [path]
    report = audit(assemble(paths))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["consistent"] else 1


if __name__ == "__main__":
    sys.exit(main())
