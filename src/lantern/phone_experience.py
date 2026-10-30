"""Phone experience packets: what a phone-local Lantern learned, for review.

The phone sends signed `experience` envelopes holding the owner's turns from a
phone-local project (`lantern_phone_experience_v1`). The packet is untrusted
data even from the owner's own paired device: every field is bounded, every
turn digest is recomputed, and concept proposals are derived here with
`lantern.learning.concepts` rather than taken from the phone. The result is a
review-only package; nothing is imported into memory or applied.

Packets may also carry the phone's Gate 7 learning ledger
(`lantern_phone_learning_v1`): hash-chained ratings, trials, evaluations,
adoptions and reverts. Each event's digest and its link to the previous event
are re-verified here; the decisions are filed for review, never replayed into
authoritative state.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import re

from lantern.learning.concepts import OwnerTurn, concept_proposals

PACKET_FORMAT = "lantern_phone_experience_v1"
REVIEW_FORMAT = "lantern_phone_experience_review_v1"
TURN_KINDS = frozenset({"note", "goal", "constraint", "decision"})
TRAITS = frozenset({
    "curiosity", "directness", "explanation_detail", "humor",
    "playfulness", "social_energy", "warmth",
})
MAXIMUM_TURNS = 200
MAXIMUM_LEARNING_EVENTS = 500
LEARNING_KINDS = frozenset({"rating", "trial", "stop", "evaluation", "adopt", "revert", "reject"})
MAXIMUM_TEXT = 8000
_HEX = re.compile(r"[0-9a-f]{64}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def turn_digest(project: str, sequence: int, kind: str, text: str, at: str) -> str:
    """Identical to the phone's `PhoneProject.withTurn` digest."""
    return hashlib.sha256(_canonical({
        "at": at, "kind": kind, "project": project, "sequence": sequence, "text": text,
    })).hexdigest()


def learning_digest(sequence: int, kind: str, at: str, data: dict[str, object], previous: str | None) -> str:
    """Identical to the phone's `LearningEvent.digestOf`."""
    return hashlib.sha256(_canonical({
        "at": at, "data": data, "kind": kind, "previous": previous, "sequence": sequence,
    })).hexdigest()


def validate_learning(events: object) -> list[dict[str, object]]:
    """Check bounds, digests and the chain within one packet's ledger slice."""
    if type(events) is not list or len(events) > MAXIMUM_LEARNING_EVENTS:
        raise ValueError(f"experience learning must be a list of at most {MAXIMUM_LEARNING_EVENTS} events")
    checked: list[dict[str, object]] = []
    for event in events:
        if type(event) is not dict or set(event) != {"at", "data", "digest", "kind", "previous", "sequence"}:
            raise ValueError("learning event has unexpected fields")
        sequence, kind, at, data, previous = (event[k] for k in ("sequence", "kind", "at", "data", "previous"))
        if type(sequence) is not int or sequence < 0 or kind not in LEARNING_KINDS or type(data) is not dict:
            raise ValueError("learning event sequence, kind or data is invalid")
        if type(at) is not str or not _TIME.fullmatch(at) or len(_canonical(data)) > 8192:
            raise ValueError("learning event time or size is invalid")
        if previous is not None and (type(previous) is not str or not _HEX.fullmatch(previous)):
            raise ValueError("learning event link is invalid")
        if checked and (sequence != checked[-1]["sequence"] + 1 or previous != checked[-1]["digest"]):
            raise ValueError("learning ledger chain is broken")
        if event["digest"] != learning_digest(sequence, kind, at, data, previous):
            raise ValueError("learning event digest does not match its content")
        checked.append(dict(event))
    return checked


def _text(value: object, limit: int, field: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ValueError(f"experience {field} must be 1 to {limit} characters")
    return value


def validate_packet(body: object) -> dict[str, object]:
    """Return the packet's checked fields or raise ValueError."""
    if type(body) is not dict or body.get("format") != PACKET_FORMAT:
        raise ValueError("unsupported experience packet")
    project = _text(body.get("project"), 120, "project")
    objective = _text(body.get("objective"), 500, "objective")
    survey = body.get("survey", "")
    if type(survey) is not str or len(survey) > 500:
        raise ValueError("experience survey summary is too long")
    levels = body.get("levels", {})
    if type(levels) is not dict or any(
        trait not in TRAITS or type(level) is not int or not 0 <= level <= 5
        for trait, level in levels.items()
    ):
        raise ValueError("experience personality levels are invalid")
    turns = body.get("turns")
    learning = validate_learning(body.get("learning", []))
    if type(turns) is not list or len(turns) > MAXIMUM_TURNS or not (turns or learning):
        raise ValueError(f"experience packet needs up to {MAXIMUM_TURNS} turns, or learning events")
    checked, sequences = [], set()
    for turn in turns:
        if type(turn) is not dict:
            raise ValueError("experience turn must be an object")
        kind, at, sequence = turn.get("kind"), turn.get("at"), turn.get("sequence")
        text = _text(turn.get("text"), MAXIMUM_TEXT, "turn text")
        if kind not in TURN_KINDS or type(at) is not str or not _TIME.fullmatch(at):
            raise ValueError("experience turn kind or time is invalid")
        if type(sequence) is not int or sequence < 0 or sequence in sequences:
            raise ValueError("experience turn sequence is invalid")
        digest = turn.get("digest")
        if type(digest) is not str or not _HEX.fullmatch(digest) or digest != turn_digest(project, sequence, kind, text, at):
            raise ValueError("experience turn digest does not match its content")
        sequences.add(sequence)
        checked.append({"at": at, "digest": digest, "kind": kind, "sequence": sequence, "text": text})
    checked.sort(key=lambda item: item["sequence"])
    return {"learning": learning, "levels": dict(sorted(levels.items())), "objective": objective,
            "project": project, "survey": survey, "turns": checked}


def review_package(body: object, *, envelope: str, sender: str, received_at: str) -> dict[str, object]:
    """A review-only package: checked turns plus Workstation-derived concept proposals."""
    packet = validate_packet(body)
    turns = tuple(OwnerTurn(t["digest"], t["text"], t["at"]) for t in packet["turns"])
    proposals = [asdict(p) for p in concept_proposals(str(packet["project"]), turns)]
    return {
        "applied": False,
        "concept_proposals": proposals,
        "envelope": envelope,
        "format": REVIEW_FORMAT,
        "received_at": received_at,
        "sender": sender,
        "status": "review_only",
        **packet,
    }
