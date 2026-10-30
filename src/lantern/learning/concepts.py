"""Concept-schema candidates from owner turns (Gate 7, review-only).

A concept is a word the owner keeps returning to inside one project. Once it
appears in at least two distinct owner turns it becomes a `create` proposal in
the `project_concepts` scope, carrying its mention count, the concepts it
co-occurs with, and the exact turns as evidence. Proposals are never applied;
`propose_schema_change` validates them and refuses protected scopes.

The phone runs an identical port (`mobile/.../phone/PhoneConcepts.kt`); shared
vectors prove both produce the same proposals.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from lantern.learning.schemas import SchemaChangeProposal, propose_schema_change

SCOPE = "project_concepts"
MINIMUM_TURNS = 2
MAXIMUM_PROPOSALS = 12
MAXIMUM_RELATED = 3
_WORD = re.compile(r"[a-z][a-z'-]{3,}")
_SLUG = re.compile(r"[^a-z0-9]+")
STOPWORDS = frozenset({
    "about", "after", "again", "also", "because", "been", "before", "being", "could",
    "does", "doing", "from", "have", "having", "here", "into", "just", "keep", "lantern",
    "like", "make", "more", "most", "much", "need", "only", "other", "over", "really",
    "same", "should", "some", "than", "that", "their", "them", "then", "there", "these",
    "they", "thing", "things", "this", "those", "very", "want", "were", "what", "when",
    "where", "which", "while", "will", "with", "would", "your", "yours",
})


@dataclass(frozen=True, slots=True)
class OwnerTurn:
    digest: str
    text: str
    at: str


def concept_words(text: str) -> tuple[str, ...]:
    """Distinct candidate concept words in one turn, in first-seen order."""
    seen: list[str] = []
    # lower(), not casefold(): Kotlin's lowercase() has no full case folding.
    for word in _WORD.findall(text.lower()):
        word = word.strip("'-")
        if len(word) >= 4 and word not in STOPWORDS and word not in seen:
            seen.append(word)
    return tuple(seen)


def project_slug(name: str) -> str:
    return _SLUG.sub("-", name.lower()).strip("-")[:40] or "project"


def concept_candidates(project: str, turns: tuple[OwnerTurn, ...]) -> list[dict[str, object]]:
    """Deterministic candidate payloads, most-mentioned first, then alphabetical."""
    mentions: dict[str, list[OwnerTurn]] = {}
    together: dict[str, dict[str, int]] = {}
    for turn in turns:
        words = concept_words(turn.text)
        for word in words:
            mentions.setdefault(word, []).append(turn)
            for other in words:
                if other != word:
                    together.setdefault(word, {})[other] = together.setdefault(word, {}).get(other, 0) + 1
    eligible = [w for w, items in mentions.items() if len(items) >= MINIMUM_TURNS]
    eligible.sort(key=lambda w: (-len(mentions[w]), w))
    candidates = []
    for word in eligible[:MAXIMUM_PROPOSALS]:
        related = sorted(
            (other for other in together.get(word, {}) if other in eligible),
            key=lambda other: (-together[word][other], other),
        )[:MAXIMUM_RELATED]
        items = mentions[word]
        candidates.append({
            "after": {
                "first_seen": items[0].at,
                "last_seen": items[-1].at,
                "mentions": len(items),
                "related": related,
                "term": word,
            },
            "evidence_references": [item.digest for item in items],
            "reason": f"'{word}' appeared in {len(items)} owner turns in {project}.",
            "schema_reference": f"concept:{project_slug(project)}:{word}",
        })
    return candidates


def concept_proposals(project: str, turns: tuple[OwnerTurn, ...]) -> list[SchemaChangeProposal]:
    """Validated, unapplied `create` proposals for the candidates."""
    return [
        propose_schema_change(
            operation="create",
            scope=SCOPE,
            schema_references=(candidate["schema_reference"],),
            before={},
            after=candidate["after"],
            evidence_references=tuple(candidate["evidence_references"]),
            reason=str(candidate["reason"]),
        )
        for candidate in concept_candidates(project, turns)
    ]
