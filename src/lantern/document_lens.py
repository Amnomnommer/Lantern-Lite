"""Deterministic document lenses for Lantern's reading pipeline.

The lens does not decide whether a claim is true.  It records how a document is
written so later minds can treat an encyclopedia, play, manual, and conversation
differently without retaining the raw document.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class DocumentKind(str, Enum):
    REFERENCE = "reference"
    SCIENTIFIC = "scientific"
    NEWS = "news"
    ARGUMENT = "argument"
    MANUAL = "manual"
    NARRATIVE = "narrative"
    PLAY = "play"
    TRANSCRIPT = "transcript"
    PERSONAL_ESSAY = "personal_essay"
    POETRY = "poetry"
    DICTIONARY = "dictionary"
    STRUCTURED_DATA = "structured_data"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DocumentLens:
    kind: DocumentKind
    confidence: float
    evidence: tuple[str, ...]
    conversational_voice: bool
    claim_mode: str


_CUES = {
    DocumentKind.REFERENCE: (("encyclopedia", 4), ("references", 2), ("citation", 1), ("overview", 1)),
    DocumentKind.SCIENTIFIC: (("abstract", 3), ("method", 2), ("results", 2), ("doi", 3), ("et al.", 2)),
    DocumentKind.NEWS: (("reported", 2), ("according to", 2), ("breaking", 2), ("updated", 1), ("dateline", 2)),
    DocumentKind.ARGUMENT: (("therefore", 2), ("I argue", 3), ("thesis", 2), ("because", 1), ("in conclusion", 2)),
    DocumentKind.MANUAL: (("step 1", 3), ("warning", 2), ("instructions", 2), ("install", 1), ("configuration", 1)),
    DocumentKind.NARRATIVE: (("chapter", 2), ("once upon", 3), ("said", 1), ("narrator", 2)),
    DocumentKind.PLAY: (("act i", 4), ("scene i", 4), ("dramatis personae", 5), ("stage direction", 3)),
    DocumentKind.TRANSCRIPT: (("transcript", 4), ("speaker:", 3), ("interviewer:", 3), ("[applause]", 2)),
    DocumentKind.PERSONAL_ESSAY: (("I remember", 3), ("my experience", 3), ("I felt", 2), ("memoir", 2)),
    DocumentKind.POETRY: (("stanza", 3), ("verse", 2), ("rhyme", 1)),
    DocumentKind.DICTIONARY: (("etymology", 3), ("pronunciation", 3), ("noun", 1), ("synonym", 2)),
    DocumentKind.STRUCTURED_DATA: (("{", 1), ("\"type\":", 3), ("<table", 3), ("csv", 2)),
}


def _cue(cue: str) -> re.Pattern[str]:
    """A cue as a whole word (a plural counts) where it starts or ends with a letter or digit: "verse" never fires
    inside "reversed" (owner report, 2026-10-29: a page about the US flag read as poetry)."""
    text = cue.casefold()
    return re.compile(("(?<![a-z0-9])" if text[:1].isalnum() else "") + re.escape(text)
                      + ("(?:e?s)?(?![a-z0-9])" if text[-1:].isalpha() else "(?![a-z0-9])" if text[-1:].isdigit() else ""))


def classify_document(text: str, *, title: str = "", media_type: str = "") -> DocumentLens:
    sample = (title + "\n" + text[:20_000]).strip()
    lower = sample.casefold()
    scores: dict[DocumentKind, int] = {kind: 0 for kind in _CUES}
    evidence: dict[DocumentKind, list[str]] = {kind: [] for kind in _CUES}
    for kind, cues in _CUES.items():
        for cue, weight in cues:
            if _cue(cue).search(lower):
                scores[kind] += weight
                evidence[kind].append(cue)
    if re.search(r"(?m)^[A-Z][A-Z .'-]{1,30}:\s+", sample):
        scores[DocumentKind.PLAY] += 3
        evidence[DocumentKind.PLAY].append("character-labelled dialogue")
    if re.search(r"(?m)^(?:Q|A|HOST|GUEST|SPEAKER \d+):\s+", sample, re.I):
        scores[DocumentKind.TRANSCRIPT] += 4
        evidence[DocumentKind.TRANSCRIPT].append("speaker turns")
    if media_type.casefold() in {"application/json", "text/csv", "application/xml"}:
        scores[DocumentKind.STRUCTURED_DATA] += 6
        evidence[DocumentKind.STRUCTURED_DATA].append(media_type.casefold())
    ranked = sorted(scores, key=lambda k: (-scores[k], k.value))
    best = ranked[0]
    top = scores[best]
    if top == 0:
        best = DocumentKind.UNKNOWN
        confidence = 0.0
        found: tuple[str, ...] = ()
    else:
        second = scores[ranked[1]]
        confidence = round(min(0.99, 0.5 + (top - second) / max(2 * top, 1)), 3)
        found = tuple(evidence[best])
    conversational = best in {DocumentKind.TRANSCRIPT, DocumentKind.PERSONAL_ESSAY}
    mode = {
        DocumentKind.PLAY: "fictional_utterance",
        DocumentKind.NARRATIVE: "narrated_world",
        DocumentKind.POETRY: "figurative_expression",
        DocumentKind.ARGUMENT: "attributed_argument",
        DocumentKind.TRANSCRIPT: "attributed_speech",
        DocumentKind.MANUAL: "instruction",
        DocumentKind.STRUCTURED_DATA: "record",
    }.get(best, "proposition")
    return DocumentLens(best, confidence, found, conversational, mode)

