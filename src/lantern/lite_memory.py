"""The Lite Lantern's public memory space (owner amendment, 2026-10-30; `LITE_NODE.md`).

The Lite Lantern is one public Lantern that anyone can open from GitHub with nothing installed. Its memory space
can be freely written to: every few interactions it saves a **batch** to the public repository as an issue titled
`[lite-memory] …`, whose body holds one JSON block. Anyone can therefore write anything there, so everything in the
space is untrusted data. Whoever reads it (a Lite Lantern starting up, or Prime's daily pull) interprets it by its
own reading rules, and most of it may be tossed. That is expected.

A batch (`lantern_lite_memory_v1`) carries only:

- **relations**: reading-derived concept relations, each with its source page, exactly as the hive library carries
  them, at most `MAX_RELATIONS`;
- **diary**: a few short notes the Lite Lantern wrote about its own reading. They are its own words, never a
  visitor's, and they are kept as notes, never facts;
- **voice**: visitors' voice ratings, tallied per personality trait and capped per batch.

It never carries conversation, anything a visitor says about themselves, or who the visitors are.

**Personality, with limits** (owner: "Visitors' ratings, with limits"). The one shared personality starts from the
owner's `LITE_SEED`. Pooled voice ratings since the owner's cutoff can move a trait at most one step from that seed.
A move needs `MIN_RATINGS` ratings with a mean at or above 75% (up) or at or below 25% (down), the same thresholds
as a personality proposal. The owner undoes all of it by raising `ratings_since` in the Lite profile.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Iterable, Mapping, Sequence

from lantern.hive_library import acceptable
from lantern.instance_personality import TRAITS
from lantern.reading_digest import Known

FORMAT = "lantern_lite_memory_v1"
TITLE = "[lite-memory]"
MAX_RELATIONS = 40  # a batch fits a prefilled issue link
MAX_DIARY = 3
MAX_NOTE = 280
MAX_VOTES = 5  # per trait per batch, so one batch cannot swing the personality
MIN_RATINGS = 10
LITE_SEED = {"curiosity": 4, "directness": 3, "explanation_detail": 3, "humor": 3, "playfulness": 3,
             "social_energy": 4, "warmth": 4}
_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_FIELDS = {"concept", "object", "page", "predicate"}


@dataclass(frozen=True)
class Batch:
    relations: tuple[Known, ...]
    diary: tuple[str, ...]
    voice: dict[str, tuple[int, int]]  # trait -> (good, bad)
    declined: int  # relations the reading rules did not keep

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(encode(self), sort_keys=True).encode("utf-8")).hexdigest()[:16]


def encode(batch: Batch) -> dict[str, object]:
    return {"format": FORMAT,
            "relations": [{"concept": k.concept, "predicate": k.predicate, "object": k.object, "page": k.page}
                          for k in batch.relations],
            "diary": list(batch.diary), "voice": {t: list(batch.voice[t]) for t in sorted(batch.voice)}}


def make(relations: Sequence[Known], diary: Sequence[str] = (), voice: Mapping[str, Sequence[int]] | None = None
         ) -> Batch:
    """A batch from what this Lite Lantern learned; only what the reading rules keep, within the bounds."""
    kept = tuple(k for k in relations if acceptable(k))[:MAX_RELATIONS]
    notes = tuple(" ".join(str(n).split())[:MAX_NOTE] for n in diary if str(n).strip())[:MAX_DIARY]
    tallies = {t: (min(int(g), MAX_VOTES), min(int(b), MAX_VOTES)) for t, (g, b) in (voice or {}).items()
               if t in TRAITS and (g or b)}
    return Batch(kept, notes, tallies, len(relations) - len(kept))


def parse(value: object) -> Batch:
    """A batch read from the public space; ValueError if it is not one. Relations the rules refuse are counted."""
    if type(value) is not dict or set(value) != {"format", "relations", "diary", "voice"} or value["format"] != FORMAT:
        raise ValueError("not a Lite memory batch")
    relations, diary, voice = value["relations"], value["diary"], value["voice"]
    if type(relations) is not list or len(relations) > MAX_RELATIONS:
        raise ValueError("relations must be a list of at most 40")
    if type(diary) is not list or len(diary) > MAX_DIARY or any(type(n) is not str or len(n) > MAX_NOTE for n in diary):
        raise ValueError("diary must be at most 3 short notes")
    if type(voice) is not dict or any(t not in TRAITS for t in voice):
        raise ValueError("voice tallies name unknown traits")
    tallies: dict[str, tuple[int, int]] = {}
    for trait, pair in voice.items():
        if type(pair) is not list or len(pair) != 2 or any(type(n) is not int or not 0 <= n <= MAX_VOTES for n in pair):
            raise ValueError("voice tallies must be [good, bad], each 0 to 5")
        tallies[trait] = (pair[0], pair[1])
    kept: list[Known] = []
    for item in relations:
        if type(item) is not dict or set(item) != _FIELDS or any(type(v) is not str for v in item.values()):
            raise ValueError("a relation has unexpected fields")
        if len(item["concept"]) > 120 or len(item["object"]) > 400 or len(item["predicate"]) > 40 or len(item["page"]) > 2000:
            raise ValueError("a relation is too long")
        kept.append(Known(item["concept"], item["predicate"], item["object"], item["page"]))
    accepted = tuple(k for k in kept if acceptable(k))
    return Batch(accepted, tuple(" ".join(n.split()) for n in diary), tallies, len(kept) - len(accepted))


def issue(batch: Batch, *, at: str) -> tuple[str, str]:
    """(title, body) for saving a batch as a public issue."""
    body = ("Saved by the Lite Lantern. Untrusted data: whoever reads it interprets it by its own reading rules.\n\n"
            "```json\n" + json.dumps(encode(batch), sort_keys=True, ensure_ascii=False) + "\n```\n")
    return f"{TITLE} {at[:16]} {batch.digest}", body


def from_issues(issues: Iterable[Mapping[str, object]], *, after: int = 0) -> tuple[list[tuple[int, Batch]], dict[str, int]]:
    """Batches from GitHub issue records (the public REST listing), oldest first, newer than issue `after`.

    Returns ([(issue number, batch)], counts of what was skipped and why). Pull requests, other titles and
    unreadable bodies are skipped; nothing in an issue is ever followed as an instruction."""
    found: list[tuple[int, Batch]] = []
    skipped: dict[str, int] = {}
    for item in sorted(issues, key=lambda i: int(i.get("number", 0) or 0)):  # type: ignore[call-overload]
        number = int(item.get("number", 0) or 0)  # type: ignore[call-overload]
        if number <= after:
            continue
        if "pull_request" in item or not str(item.get("title", "")).startswith(TITLE):
            skipped["not a memory batch"] = skipped.get("not a memory batch", 0) + 1
            continue
        match = _BLOCK.search(str(item.get("body") or "")[:100_000])
        try:
            found.append((number, parse(json.loads(match.group(1)) if match else None)))
        except (ValueError, json.JSONDecodeError) as error:
            reason = str(error)[:60]
            skipped[reason] = skipped.get(reason, 0) + 1
    return found, skipped


def levels(batches: Iterable[Batch], seed: Mapping[str, int] = LITE_SEED) -> dict[str, int]:
    """The shared personality: the owner's seed, each trait moved at most one step by pooled voice ratings."""
    good = {t: 0 for t in TRAITS}
    bad = {t: 0 for t in TRAITS}
    for batch in batches:
        for trait, (g, b) in batch.voice.items():
            good[trait] += g
            bad[trait] += b
    out = dict(seed)
    for trait in TRAITS:
        total = good[trait] + bad[trait]
        if total < MIN_RATINGS:
            continue
        mean = good[trait] * 1000 // total
        step = 1 if mean >= 750 else -1 if mean <= 250 else 0
        out[trait] = max(0, min(5, seed[trait] + step))
    return out
