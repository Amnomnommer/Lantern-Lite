"""Prime's own Gate 7 learning in conversation: a port of the phone's PhoneLearning.

The owner rates replies (`!good` / `!bad`, or implicitly by reacting with "haha"
or "huh?"); a reply strategy runs as an A/B trial beside the usual reply;
evaluation compares the two across at least two conversation days; only a
retained strategy can be adopted, only by the owner, and every adoption can be
reverted. The ledger has the phone's format (`lantern_phone_learning_v1`), so the
same auditor (`lantern.learning.phone_ledger.audit`) checks Prime's ledger too.

Messages, ledger events and reply shaping match the phone exactly
(`mobile/app/src/test/resources/prime-learning-vectors-v1.json`). Adoption changes
only how Prime phrases replies; it never writes Prime's memory, goals or
permissions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from lantern.companion import EMPTY_LIBRARY, Library, Reply
from lantern.learning.concepts import OwnerTurn, concept_candidates, concept_words, project_slug
from lantern.learning.phone_ledger import (
    STARTING_CONFIDENCE,
    STRATEGIES,
    LearningView,
    evaluation_data,
    view,
)
from lantern.phone_experience import learning_digest

MAX_EVENTS = 5000
DESCRIPTIONS = {
    "concept_link": "link a note to a recurring idea in the project",
    "concept_recall": "recall your earlier words about the idea a question is about",
    "phrasebook": "reply the way people replied in what I read",
    "reading_link": "share something I read that connects to what you said",
}
# Strategies Lantern starts and judges by itself once reading gives it material.
AUTOMATIC = (("phrasebook", 20), ("reading_link", 50))
AUTO_EVALUATE_EVERY = 3
CONVERSATION_PREFIX = "Conversation "
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
PLEASED = ("haha", "hehe", "lol", "aww", "love that", "love it", "that's sweet", "thats sweet",
           "thank you", "thanks", "nice", "perfect", "exactly", "good point", "so true", "i like that", "yes!", "that's right")
PUZZLED = ("huh", "what?", "doesn't make sense", "does not make sense", "makes no sense",
           "that's not what i", "thats not what i", "weird", "you're confused", "youre confused", "wrong", "no that's not",
           "what do you mean")

Ledger = list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class Exchange:
    """One reply Lantern gave: the owner turn it answered, its act and the strategy that shaped it."""

    turn: OwnerTurn
    act: str
    reply: str
    rules: tuple[str, ...] = ()
    strategy: str = "baseline"


def is_conversation(name: str) -> bool:
    return name.startswith(CONVERSATION_PREFIX) and _DATE.fullmatch(name[len(CONVERSATION_PREFIX):]) is not None


def append(ledger: Sequence[Mapping[str, object]], kind: str, at: str, data: dict[str, object]) -> Ledger:
    if len(ledger) >= MAX_EVENTS:
        raise ValueError("learning ledger is full; review it before learning more")
    sequence = len(ledger)
    previous = ledger[-1]["digest"] if ledger else None
    event = {"at": at, "data": data, "digest": learning_digest(sequence, kind, at, data, previous),
             "kind": kind, "previous": previous, "sequence": sequence}
    return [*ledger, event]


def _unknown() -> str:
    return f"Unknown strategy. Available: {', '.join(STRATEGIES)}."


def _signed(millis: int) -> str:
    return ("+" if millis > 0 else "") + str(millis / 1000.0)


def rate(ledger: Ledger, exchange: Exchange | None, thread: str | None, good: bool, reason: str, at: str) -> tuple[Ledger, str]:
    if exchange is None or thread is None:
        return ledger, "There is no reply to rate yet."
    if len(reason) > 200:
        return ledger, "Keep the reason under 200 characters."
    about = "voice" if reason.lower().split(" ")[0] == "voice" else "reply"
    if about == "voice" and not exchange.rules:
        return ledger, "That reply had no personality voice to rate."
    if any(r.turn == exchange.turn.digest and r.about == about for r in view(ledger).ratings):
        return ledger, f"You already rated that {'voice' if about == 'voice' else 'reply'}."
    nxt = append(ledger, "rating", at, {
        "about": about, "act": exchange.act, "context": project_slug(thread), "reason": reason,
        "rules": list(exchange.rules), "score": 1 if good else 0, "strategy": exchange.strategy, "turn": exchange.turn.digest,
    })
    if about == "voice":
        return nxt, "Noted: you liked how that sounded." if good else "Noted: that did not sound right."
    note = "Noted as a good reply." if good else "Noted as a reply to improve."
    return nxt, note + ("" if exchange.strategy == "baseline" else f" (strategy: {exchange.strategy})")


def trial(ledger: Ledger, strategy: str, at: str) -> tuple[Ledger, str]:
    if strategy not in STRATEGIES:
        return ledger, _unknown()
    v = view(ledger)
    if strategy in v.adopted:
        return ledger, f"{strategy} is already adopted."
    if strategy in v.trials:
        return ledger, f"{strategy} is already in trial."
    return append(ledger, "trial", at, {"id": strategy}), (
        f"Trial started: {strategy} ({DESCRIPTIONS[strategy]}). I will alternate it with my usual reply; rate both with "
        f"!good or !bad in at least two projects, then !evaluate {strategy}."
    )


def stop(ledger: Ledger, strategy: str, at: str) -> tuple[Ledger, str]:
    if strategy not in view(ledger).trials:
        return ledger, f"{strategy} is not in trial."
    return append(ledger, "stop", at, {"id": strategy}), f"Trial stopped: {strategy}. Its ratings stay in the ledger."


def evaluate(ledger: Ledger, strategy: str, at: str) -> tuple[Ledger, str]:
    if strategy not in STRATEGIES:
        return ledger, _unknown()
    data = evaluation_data(ledger, strategy)
    if data is None:
        return ledger, (
            f"Not enough ratings yet: rate both my usual replies and {strategy} replies in the same project."
        )
    issues = data["issues"]
    if data["decision"] == "retain_candidate":
        outcome = f"retain — you can !adopt {strategy}"
    else:
        outcome = "reverse — keep my usual reply" + ("" if not issues else f" ({', '.join(issues)})")
    message = (
        f"Evaluated {strategy} across {len(data['trials'])} project(s): mean change {_signed(data['mean_improvement_millis'])}, "
        f"confidence {data['confidence_before'] / 10.0}% → {data['confidence_after'] / 10.0}%. Decision: {outcome}."
    )
    return append(ledger, "evaluation", at, data), message


def adopt_strategy(ledger: Ledger, strategy: str, at: str, *, where: str = "on this phone") -> tuple[Ledger, str]:
    if strategy not in STRATEGIES:
        return ledger, _unknown()
    v = view(ledger)
    if strategy in v.adopted:
        return ledger, f"{strategy} is already adopted."
    evaluation = v.evaluations.get(strategy)
    if evaluation is None or evaluation["data"]["decision"] != "retain_candidate":
        return ledger, (
            f"{strategy} has not passed evaluation. Run a trial, rate in two projects, then !evaluate {strategy}."
        )
    return (append(ledger, "adopt", at, {"evaluation": evaluation["digest"], "id": strategy, "target": "strategy"}),
            f"Adopted {strategy} {where}. !revert {strategy} undoes it.")


def revert_strategy(ledger: Ledger, strategy: str, at: str) -> tuple[Ledger, str]:
    if strategy not in view(ledger).adopted:
        return ledger, f"{strategy} is not adopted."
    return (append(ledger, "revert", at, {"id": strategy, "target": "strategy"}),
            f"Reverted {strategy}; I am back to my usual reply.")


def reaction(text: str) -> tuple[bool, str] | None:
    """How the owner's next message reacts to Lantern's last reply, or None when it is not clearly either."""
    n = " " + " ".join(text.lower().split()) + " "
    for cue in PUZZLED:
        if cue in n:
            return False, cue
    for cue in PLEASED:
        if cue in n:
            return True, cue
    return None


def auto_trials(ledger: Ledger, library: Library, at: str) -> Ledger:
    nxt = ledger
    v = view(ledger)
    for strategy, needed in AUTOMATIC:
        material = len(library.phrases) if strategy == "phrasebook" else len(library.known)
        dismissed = any(e["kind"] in ("stop", "revert") and e["data"].get("id") == strategy for e in ledger)
        if material >= needed and strategy not in v.trials and strategy not in v.adopted and not dismissed:
            nxt = append(nxt, "trial", at, {"by": "lantern", "id": strategy})
    return nxt


def auto_evaluate(ledger: Ledger, at: str) -> tuple[Ledger, str | None]:
    """Judge running trials as ratings arrive: stop a clearly worse one; offer a retained one to the owner."""
    nxt = ledger
    notice = None
    for strategy in sorted(view(ledger).trials):
        last_eval = max((i for i, e in enumerate(nxt) if e["kind"] == "evaluation" and e["data"].get("strategy") == strategy),
                        default=-1)
        positions = {e["digest"]: i for i, e in enumerate(nxt)}
        fresh = sum(1 for r in view(nxt).ratings if r.strategy == strategy and positions[r.digest] > last_eval)
        if fresh < AUTO_EVALUATE_EVERY:
            continue
        evaluated, _ = evaluate(nxt, strategy, at)
        if len(evaluated) == len(nxt):
            continue
        nxt = evaluated
        data = nxt[-1]["data"]
        if data["decision"] == "retain_candidate":
            notice = (f"I've noticed my {strategy.replace('_', ' ')} replies are going better than my usual ones. "
                      f"Say !adopt {strategy} to keep them.")
        elif not data["issues"] and data["mean_improvement_millis"] < 0:
            nxt = append(nxt, "stop", at, {"by": "lantern", "id": strategy})
    return nxt, notice


def _concepts_in(thread: str, turns: Sequence[OwnerTurn], turn: OwnerTurn, v: LearningView) -> list[dict[str, object]]:
    words = set(concept_words(turn.text))
    found = [c for c in concept_candidates(thread, tuple(turns))
             if c["after"]["term"] in words and c["schema_reference"] not in v.rejected_concepts]
    return sorted(found, key=lambda c: 0 if c["schema_reference"] in v.adopted_concepts else 1)


def _concept_link(thread: str, turns: Sequence[OwnerTurn], turn: OwnerTurn, v: LearningView) -> str | None:
    found = _concepts_in(thread, turns, turn, v)
    if not found:
        return None
    after = found[0]["after"]
    term, mentions, related = after["term"], after["mentions"], after["related"]
    alongside = "" if not related else f", alongside {' and '.join(related)}"
    if is_conversation(thread):
        return (f"That connects to {term}, which you have brought up {mentions} times today{alongside}. "
                f"What is it about {term} that keeps coming up?")
    return (f"I have kept that in {thread.strip()}. It connects to {term}, which you have raised in "
            f"{mentions} messages{alongside}. Is that connection what matters here?")


def _concept_recall(baseline: str, thread: str, turns: Sequence[OwnerTurn], turn: OwnerTurn, v: LearningView) -> str | None:
    for concept in _concepts_in(thread, turns, turn, v):
        term = concept["after"]["term"]
        earlier = next((t for t in reversed(turns) if t != turn and term in concept_words(t.text)), None)
        if earlier is not None:
            return f'{baseline} Earlier you said about {term}: "{earlier.text}"'
    return None


def shape(reply: Reply, thread: str, turns: Sequence[OwnerTurn], turn: OwnerTurn, v: LearningView,
          library: Library = EMPTY_LIBRARY) -> tuple[str, str]:
    """Pick the strategy for this reply: adopted always, trials on alternate turns."""
    turn_index = list(turns).index(turn)
    for strategy, act in STRATEGIES.items():
        if act != reply.act:
            continue
        if not (strategy in v.adopted or (strategy in v.trials and turn_index % 2 == 1)):
            continue
        shaped: str | None
        if strategy == "concept_link":
            shaped = _concept_link(thread, turns, turn, v)
        elif strategy == "concept_recall":
            shaped = _concept_recall(reply.text, thread, turns, turn, v)
        elif strategy == "phrasebook":
            phrase = library.phrase_for(turn.text)
            shaped = None if phrase is None else phrase.reply
        else:
            # What reading taught, interpreted into ideas (not a quoted sentence).
            understood = library.understanding(turn.text)
            shaped = None if understood is None else (
                "That reminds me of what I read: " + understood.removeprefix("From my reading: ")
            )
        if shaped is not None:
            return shaped, strategy
    return reply.text, "baseline"


def summary(ledger: Ledger, *, label: str) -> str:
    v = view(ledger)
    lines = [f"What I am learning ({label}):"]
    for strategy, description in DESCRIPTIONS.items():
        status = "adopted" if strategy in v.adopted else "in trial" if strategy in v.trials else "available"
        rated = sum(1 for r in v.ratings if r.about == "reply" and r.strategy == strategy)
        decision = v.evaluations.get(strategy)
        last = "" if decision is None else f", last evaluation: {decision['data']['decision']}"
        confidence = v.confidence.get(strategy, STARTING_CONFIDENCE) / 10.0
        lines.append(f"• {strategy}: {status}, {rated} rating(s), confidence {confidence}%{last} — {description}")
    usual = sum(1 for r in v.ratings if r.about == "reply" and r.strategy == "baseline")
    lines.append(f"• my usual replies: {usual} rating(s)")
    lines.append(f"Ledger: {len(ledger)} event(s).")
    return "\n".join(lines)
