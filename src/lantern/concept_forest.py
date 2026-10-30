"""Schema-v2 concept frames and adaptive, rebuildable concept forests.

This is an index over claims, never an authority over them.  Missing evidence is
UNKNOWN rather than NO.  The forest is derived, bounded, atomically replaceable,
and can always be rebuilt from Lantern's existing provenance-bearing memory.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Iterable


class Answer(str, Enum):
    YES = "yes"
    NO = "no"
    MAYBE = "maybe"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Assertion:
    concept: str
    predicate: str
    object: str
    source: str = ""
    confidence: float = 1.0


@dataclass
class ConceptFrame:
    concept: str
    definitions: set[str] = field(default_factory=set)
    broader: set[str] = field(default_factory=set)
    components: set[str] = field(default_factory=set)
    aspects: set[str] = field(default_factory=set)
    functions: set[str] = field(default_factory=set)
    synonyms: set[str] = field(default_factory=set)
    confusions: set[str] = field(default_factory=set)
    contexts: set[str] = field(default_factory=set)
    related: set[str] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)

    def serial(self) -> dict[str, object]:
        return {"concept": self.concept, **{k: sorted(v) for k, v in asdict(self).items() if k != "concept"}}


_SLOT = {
    "is": "definitions", "means": "definitions", "refers_to": "definitions",
    "is_a": "broader", "was_a": "broader", "consists_of": "components",
    "contains": "components", "includes": "components", "has": "aspects",
    "can": "functions", "used_for": "functions", "used_to": "functions",
    "helps": "functions", "is_called": "synonyms", "is_not": "confusions",
    "part_of": "contexts", "studies": "contexts", "involves": "related",
    "causes": "related", "produces": "related", "describes": "related",
}


def _clean(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def build_frames(assertions: Iterable[Assertion]) -> dict[str, ConceptFrame]:
    frames: dict[str, ConceptFrame] = {}
    seen: set[tuple[str, str, str, str]] = set()
    for claim in assertions:
        concept, predicate, obj = _clean(claim.concept), _clean(claim.predicate), _clean(claim.object)
        if not concept or not predicate or not obj:
            continue
        key = concept, predicate, obj, claim.source
        if key in seen:
            continue
        seen.add(key)
        frame = frames.setdefault(concept, ConceptFrame(concept))
        slot = _SLOT.get(predicate, "related")
        getattr(frame, slot).add(obj)
        if claim.source:
            frame.evidence.add(claim.source)
        if slot in {"broader", "components", "synonyms", "confusions", "contexts", "related"}:
            frame.related.add(obj)
    return frames


def _features(frame: ConceptFrame) -> set[str]:
    result = set()
    for slot in ("broader", "components", "aspects", "functions", "synonyms", "confusions", "contexts", "related"):
        result.update(f"{slot}:{value}" for value in getattr(frame, slot))
    return result


def _question(feature: str) -> str:
    slot, value = feature.split(":", 1)
    words = {"broader": "a kind of", "components": "made up of", "aspects": "characterized by",
             "functions": "able or used", "synonyms": "also called", "confusions": "distinguished from",
             "contexts": "associated with the context", "related": "directly related to"}
    return f"Is it {words[slot]} {value}?"


MIN_SHARE = 16  # a feature question must put at least 1/16 of the ideas on its smaller side


def _node(names: list[str], features: dict[str, frozenset[str]]) -> dict[str, object]:
    """One question that splits `names`, built in bounded time (2026-10-25: the first version took 26 s for
    500 ideas and recursed once per idea). Each level counts every feature once; a feature question is
    used only when it splits the ideas reasonably evenly, otherwise the names are split in half, so the
    tree is at most about 150 questions deep for Prime's ~12,000 ideas and stays lossless."""
    if len(names) == 1:
        return {"concept": names[0]}
    counts: dict[str, int] = {}
    for name in names:
        for feature in features[name]:
            counts[feature] = counts.get(feature, 0) + 1
    smallest = max(1, len(names) // MIN_SHARE)
    best = None
    best_key: tuple[int, str] | None = None
    for feature, yes in counts.items():
        if smallest <= yes <= len(names) - smallest:
            key = (abs(len(names) - 2 * yes), feature)
            if best_key is None or key < best_key:
                best, best_key = feature, key
    if best is None:
        # Names are the final lossless discriminator: halves, so depth grows with log(memory), never with memory.
        middle = len(names) // 2
        return {"question": f"Does its canonical name come before {names[middle]}?", "feature": f"name<{names[middle]}",
                "yes": _node(names[:middle], features), "unknown": _node(names[middle:], features)}
    yes_names = [n for n in names if best in features[n]]
    unknown_names = [n for n in names if best not in features[n]]
    return {"question": _question(best), "feature": best,
            "yes": _node(yes_names, features), "unknown": _node(unknown_names, features)}


def build_forest(assertions: Iterable[Assertion]) -> dict[str, object]:
    frames = build_frames(assertions)
    names = sorted(frames)
    features = {n: frozenset(_features(frames[n])) for n in names}
    tree = None if not names else _node(names, features)
    payload: dict[str, object] = {"format": 2, "logic": [a.value for a in Answer],
                                  "frames": [frames[n].serial() for n in names], "tree": tree}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["sha256"] = hashlib.sha256(canonical).hexdigest()
    validate_forest(payload)
    return payload


def validate_forest(forest: dict[str, object]) -> None:
    frames = forest.get("frames")
    if forest.get("format") != 2 or not isinstance(frames, list):
        raise ValueError("concept_forest_format")
    names = [f.get("concept") for f in frames if isinstance(f, dict)]
    if len(names) != len(set(names)) or any(not isinstance(n, str) or not n for n in names):
        raise ValueError("concept_forest_duplicate_or_empty_concept")
    leaves: list[str] = []
    def walk(node: object) -> None:
        if node is None:
            return
        if not isinstance(node, dict):
            raise ValueError("concept_forest_node")
        if "concept" in node:
            leaves.append(node["concept"])
            return
        if not isinstance(node.get("question"), str) or "yes" not in node or "unknown" not in node:
            raise ValueError("concept_forest_question")
        walk(node["yes"]); walk(node["unknown"])
    walk(forest.get("tree"))
    if sorted(leaves) != sorted(names):
        raise ValueError("concept_forest_not_lossless")
    claimed = forest.get("sha256")
    if claimed is not None:
        unsigned = {key: value for key, value in forest.items() if key != "sha256"}
        actual = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
        if claimed != actual:
            raise ValueError("concept_forest_digest")


def related_concepts(forest: dict[str, object], concept: str, limit: int = 8) -> list[tuple[str, float]]:
    frames = {f["concept"]: f for f in forest.get("frames", [])}
    name = _clean(concept)
    if name not in frames:
        return []
    def fs(frame: dict[str, object]) -> set[str]:
        return {f"{slot}:{value}" for slot in _SLOT.values() for value in frame.get(slot, [])}
    target = fs(frames[name])
    ranked = []
    for other, frame in frames.items():
        if other == name:
            continue
        theirs = fs(frame)
        union = target | theirs
        score = 0.0 if not union else len(target & theirs) / len(union)
        if other in frames[name].get("related", []) or name in frame.get("related", []):
            score += 1.0
        if score:
            ranked.append((other, round(score, 6)))
    return sorted(ranked, key=lambda x: (-x[1], x[0]))[:limit]


def atomic_rebuild(path: Path, assertions: Iterable[Assertion], *, night: str) -> dict[str, object]:
    """Validate then atomically swap the derived index; keep the previous copy for rollback."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    forest = build_forest(assertions)
    data = json.dumps(forest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        stream.write(data)
        stream.flush(); os.fsync(stream.fileno())
        staged = Path(stream.name)
    try:
        previous = path.with_suffix(path.suffix + ".previous")
        moved_previous = False
        if path.exists():
            os.replace(path, previous)
            moved_previous = True
        try:
            os.replace(staged, path)
        except Exception:
            if moved_previous and previous.exists():
                os.replace(previous, path)
            raise
    finally:
        staged.unlink(missing_ok=True)
    return {"night": night, "concepts": len(forest["frames"]), "sha256": forest["sha256"],
            "reporting": "debug_only", "authority_changed": False}

