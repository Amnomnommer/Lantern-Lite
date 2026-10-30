"""Prime's library: what it keeps from its own reading (port of the phone's PhoneLibrary).

Owner direction (2026-10-22): reading becomes learning, as much as possible on
the backend. After each `lantern-explore read` session, the journals in the
reading workspace are the library's only source:

- Knowledge: informative sentences with their source, community and date.
  Lantern answers from these only with the source attached, never as its own fact.
- Phrasebook: speech pairs (said -> reply), used only through the Gate 7
  "phrasebook" strategy, which is trialled, rated and evaluated before the owner
  adopts it.

Pages flagged for suspicious instructions are left out entirely: web content is
untrusted data, never instructions. The library is rebuilt from the journals and
is never written into Prime's memory. Extraction matches the phone exactly
(`mobile/app/src/test/resources/library-vectors-v1.json`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from lantern.companion import KnowledgeEntry, Library, Phrase

MAX_KNOWLEDGE = 20_000
MAX_PHRASES = 5_000
SENTENCES_PER_PAGE = 40
# Java's \s is ASCII whitespace; spell it out so Python's Unicode \s does not split differently.
_SENTENCE_END = re.compile(r"(?<=[.!?])[ \t\n\x0b\f\r]+(?=[A-Z\"“])")
_PROPER_NAME = re.compile(r"(?<!^)(?<![.!?] )\b(?!I\b)[A-Z][a-z]+")


def sentences_from(url: str, community: str, retrieved_at: str, text: str) -> list[KnowledgeEntry]:
    """Informative sentences from the start of a page: 8 to 40 words, nothing redacted, no leftovers."""
    found = []
    for sentence in (part.strip() for part in _SENTENCE_END.split(text)):
        if (8 <= len(sentence.split(" ")) <= 40 and sentence[-1] in ".!?" and "[email]" not in sentence
                and "[phone]" not in sentence and "[handle]" not in sentence and "|" not in sentence):
            found.append(KnowledgeEntry(sentence, url, community, retrieved_at[:10]))
    return found[:SENTENCES_PER_PAGE]


def phrases_from(url: str, community: str, exchanges: Iterable[Mapping[str, str]]) -> list[Phrase]:
    """Speech pairs usable as replies: short, self-contained, no other characters' names."""
    return [
        Phrase(e["said"], e["reply"], url, community)
        for e in exchanges
        if len(e["said"].split(" ")) <= 20 and 2 <= len(e["reply"].split(" ")) <= 25
        and not _PROPER_NAME.search(e["reply"])
    ]


def _distinct_last(items: Sequence[object], key, limit: int) -> list:
    seen: set[object] = set()
    kept = []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            kept.append(item)
    return kept[-limit:]


def combine(library: Library, knowledge: Sequence[KnowledgeEntry], phrases: Sequence[Phrase]) -> Library:
    """Add what one session read; oldest entries go first beyond the bounds."""
    return Library(
        _distinct_last([*library.knowledge, *knowledge], lambda e: e.sentence, MAX_KNOWLEDGE),
        _distinct_last([*library.phrases, *phrases], lambda p: (p.said.lower(), p.reply.lower()), MAX_PHRASES),
    )


def from_journals(directory: Path) -> Library:
    """The library from every reading journal in `directory`, oldest session first."""
    knowledge: list[KnowledgeEntry] = []
    phrases: list[Phrase] = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
            pages = journal["pages"]
        except (OSError, ValueError, KeyError, TypeError):
            continue  # an unreadable journal teaches nothing; the rest still count
        for page in pages:
            if page.get("suspicious_instructions") or page.get("trusted_as_instructions"):
                continue
            knowledge += sentences_from(page["url"], page["community"], page["retrieved_at"], page.get("text", ""))
            phrases += phrases_from(page["url"], page["community"], page.get("exchanges", []))
    return combine(Library(), knowledge, phrases)
