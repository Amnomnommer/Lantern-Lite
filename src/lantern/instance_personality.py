"""Each Lantern's own personality (owner, 2026-10-23: "unique enough personalities").

The Personality Core is shared code; each Lantern's levels are its own instance
state. They are kept with that Lantern (Prime: inside the vault) and are never sent
through the hive.

- Starting temperaments differ. Prime, the reader at home, is curious and
  thorough. The phone, the companion in your pocket, is playful and sociable
  (`PhoneMindState.PHONE_SEED`).
- Levels change only by the owner: `!tone` by hand, or by adopting a proposal
  learned from voice ratings (`!good voice` / `!bad voice`), recorded in the same
  Gate 7 ledger as the phone and revertible.
- Interests are the ideas this Lantern's own reading taught it most about, never
  what came through the hive, so two Lanterns that read differently become
  interested in different things.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from lantern.learning.phone_ledger import personality_proposal, view
from lantern.personality_runtime import SERIOUS_ACTS, VOICE_RULES
from lantern.reading_digest import Known

FORMAT = "lantern_instance_personality_v1"
TRAITS = ("curiosity", "directness", "explanation_detail", "humor", "playfulness", "social_energy", "warmth")
PRIME_SEED = {"curiosity": 5, "directness": 3, "explanation_detail": 4, "humor": 2, "playfulness": 2,
              "social_energy": 2, "warmth": 4}
PHONE_SEED = {"curiosity": 3, "directness": 3, "explanation_detail": 2, "humor": 3, "playfulness": 4,
              "social_energy": 4, "warmth": 4}


def voice(text: str, act: str, levels: Mapping[str, int]) -> tuple[str, tuple[str, ...]]:
    """Frame a reply in these levels' voice; identical to `PhoneVoice.apply` without offsets."""
    if act == "care":
        return text, ()
    opener = closer = None
    applied: list[str] = []
    for rule in VOICE_RULES:
        if act not in rule.acts or levels.get(rule.trait, -1) < rule.minimum:
            continue
        if act in SERIOUS_ACTS and rule.trait in ("humor", "playfulness"):
            continue
        if rule.position == "opener" and opener is None:
            opener = rule
        elif rule.position == "closer" and closer is None:
            closer = rule
        else:
            continue
        applied.append(rule.rule_id)
    parts = [opener.text if opener else None, text or None, closer.text if closer else None]
    return " ".join(p for p in parts if p), tuple(applied)


def load(path: Path, seed: Mapping[str, int] = PRIME_SEED) -> dict[str, int]:
    """This Lantern's levels; the seed until the first change is saved."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(seed)
    levels = value.get("levels") if value.get("format") == FORMAT else None
    if not isinstance(levels, dict) or set(levels) != set(TRAITS) or any(
            type(v) is not int or not 0 <= v <= 5 for v in levels.values()):
        return dict(seed)
    return dict(levels)


def save(path: Path, levels: Mapping[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"format": FORMAT, "levels": dict(sorted(levels.items()))}, indent=1) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def tone(levels: Mapping[str, int], argument: str, *, where: str) -> tuple[dict[str, int], str]:
    parts = argument.split()
    direction = {"up": 1, "down": -1}.get(parts[1] if len(parts) == 2 else "", 0)
    if len(parts) != 2 or parts[0] not in TRAITS or direction == 0:
        return dict(levels), f"Use !tone trait up|down with one of: {', '.join(sorted(TRAITS))}."
    level = max(0, min(5, levels[parts[0]] + direction))
    return {**levels, parts[0]: level}, f"{parts[0]} is now {level}/5 {where}."


def proposals(ledger: Sequence[Mapping[str, object]], levels: Mapping[str, int]) -> list[dict[str, object]]:
    found = []
    for trait in sorted(levels):
        proposal = personality_proposal(ledger, trait, levels[trait])
        if proposal is not None:
            found.append({"trait": trait, **proposal})
    return found


def adopt(ledger: list, levels: Mapping[str, int], trait: str, at: str) -> tuple[list, dict[str, int], str]:
    """Port of `PhoneLearning.adoptPersonality`."""
    from lantern.learning.companion_learning import append

    proposal = next((p for p in proposals(ledger, levels) if p["trait"] == trait), None)
    if proposal is None:
        return ledger, dict(levels), f"There is no personality proposal for {trait}. See !learning."
    nxt = append(ledger, "adopt", at, {
        "after": proposal["after"], "before": proposal["before"], "evidence": proposal["evidence"],
        "mean_millis": proposal["mean_millis"], "target": "personality", "trait": trait,
    })
    return nxt, {**levels, trait: proposal["after"]}, (
        f"{trait} is now {proposal['after']}/5, learned from your ratings. !revert personality {trait} undoes it.")


def revert(ledger: list, levels: Mapping[str, int], trait: str, at: str) -> tuple[list, dict[str, int], str]:
    """Port of `PhoneLearning.revertPersonality`."""
    from lantern.learning.companion_learning import append

    adoption = view(ledger).personality_adoptions.get(trait)
    if adoption is None:
        return ledger, dict(levels), f"{trait} has no learned change to revert."
    before = int(adoption["data"]["before"])
    nxt = append(ledger, "revert", at, {
        "adoption": adoption["digest"], "after": before, "before": levels[trait], "target": "personality", "trait": trait,
    })
    return nxt, {**levels, trait: before}, f"{trait} is back to {before}/5."


def interests(known: Sequence[Known], limit: int = 5) -> list[str]:
    """The ideas this Lantern's own reading taught it most about (two facts at least); hive learning excluded."""
    counts: dict[str, int] = {}
    for k in known:
        if not k.via:
            counts[k.concept] = counts.get(k.concept, 0) + 1
    order = {name: i for i, name in enumerate(counts)}
    ranked = sorted((n for n, c in counts.items() if c >= 2), key=lambda n: (-counts[n], order[n]))
    return ranked[:limit]


def describe(levels: Mapping[str, int], known: Sequence[Known], *, label: str) -> str:
    lines = [f"My voice {label}:"] + [f"{trait}: {levels[trait]}/5" for trait in sorted(levels)]
    mine = interests(known)
    lines.append("Lately I've been reading most about: " + ", ".join(mine) + "." if mine
                 else "I haven't read enough yet to have favourite topics.")
    lines.append("This is mine alone; it never travels through the hive. (!tone trait up|down changes it.)")
    return "\n".join(lines)


# -- owner-controlled history (PERSONALITY_CORE.md; W-M2) ---------------------------------

PROFILES_FILE = "profiles.json"
VOICE_FILE = "voice.json"
BOUNDARIES = ("Personality shapes how this Lantern speaks; it never grants knowledge, permission or safety authority.",)


class PersonalityStore:
    """This Lantern's personality as Personality Core profiles: every change a new version, nothing overwritten.

    Only the owner changes it: by !tone, by adopting or reverting a proposal learned from
    voice ratings, by restoring an earlier version, or by turning the voice off and on.
    Reading, the hive, Lantern conversations and automatic learning never call it.
    """

    def __init__(self, folder: Path, *, instance_id: str = "lantern-local-instance",
                 owner_id: str = "local-owner-0001", seed: Mapping[str, int] = PRIME_SEED,
                 seed_name: str = "prime-temperament") -> None:
        self.folder = Path(folder)
        self.instance_id = instance_id
        self.owner_id = owner_id
        self.seed = dict(seed)
        self.seed_name = seed_name

    profiles_path = property(lambda self: self.folder / PROFILES_FILE)
    voice_path = property(lambda self: self.folder / VOICE_FILE)

    def history(self) -> list:
        from lantern.personality_profile import PersonalityProfile, PersonalityTrait

        try:
            values = json.loads(self.profiles_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found = []
        for value in values:
            payload = {k: v for k, v in value.items() if k not in ("format", "profile_sha256")}
            payload["traits"] = tuple(PersonalityTrait(**t) for t in payload["traits"])
            for key in ("boundaries", "provenance"):
                payload[key] = tuple(payload[key])
            profile = PersonalityProfile(**payload, profile_sha256=value["profile_sha256"]).validate()
            if profile.instance_id != self.instance_id or profile.owner_id != self.owner_id:
                raise ValueError("personality history belongs to a different instance")  # never adopt a foreign history
            if found and (profile.version != found[-1].version + 1
                          or profile.previous_profile_sha256 != found[-1].profile_sha256):
                raise ValueError("personality history is broken")
            found.append(profile)
        return found

    def _write(self, profiles: list) -> None:
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.profiles_path.with_suffix(".tmp")
        temporary.write_text(json.dumps([{**p.payload(), "profile_sha256": p.profile_sha256} for p in profiles],
                                        indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.profiles_path)

    def _add(self, levels: Mapping[str, int], reference: str, at: str) -> int:
        from lantern.personality_profile import PersonalityTrait, make_profile

        profiles = self.history()
        previous = profiles[-1] if profiles else None
        version = 1 if previous is None else previous.version + 1
        profile = make_profile(
            profile_id=f"{self.instance_id[:40]}-v{version}", instance_id=self.instance_id, owner_id=self.owner_id,
            version=version, status="accepted", created_at=at,
            traits=(PersonalityTrait(t, int(levels[t]), reference) for t in TRAITS), boundaries=BOUNDARIES,
            provenance=(reference,), previous_profile_sha256=None if previous is None else previous.profile_sha256)
        self._write([*profiles, profile])
        return version

    def levels(self, *, at: str = "2026-10-23T00:00:00+00:00") -> dict[str, int]:
        """The active levels; the first call starts the history (from older saved levels, or the seed)."""
        profiles = self.history()
        if not profiles:
            legacy = self.folder / "levels.json"
            if legacy.exists():
                self._add(load(legacy, self.seed), "migrated:instance-levels", at)
            else:
                self._add(self.seed, f"seed:{self.seed_name}", at)
            profiles = self.history()
        return {t.name: t.level for t in profiles[-1].traits}

    def change(self, levels: Mapping[str, int], reference: str, at: str) -> int:
        """Record new levels as the next version; `reference` says why (owner:tone, learning:adopt:...)."""
        self.levels(at=at)
        return self._add(levels, reference, at)

    def restore(self, version: int, at: str) -> tuple[dict[str, int], str]:
        """Bring back an earlier version's levels as a new version; history is never rewritten."""
        profiles = self.history()
        target = next((p for p in profiles if p.version == version), None)
        if target is None:
            return self.levels(at=at), f"There is no version {version}. !personality history lists them."
        levels = {t.name: t.level for t in target.traits}
        new = self._add(levels, f"owner:restore:v{version}", at)
        return levels, f"Restored version {version} as version {new}. Nothing earlier was erased."

    def enabled(self) -> bool:
        try:
            return bool(json.loads(self.voice_path.read_text(encoding="utf-8"))["enabled"])
        except (OSError, ValueError, KeyError, TypeError):
            return True

    def set_enabled(self, enabled: bool, at: str) -> str:
        try:
            events = list(json.loads(self.voice_path.read_text(encoding="utf-8"))["events"])
        except (OSError, ValueError, KeyError, TypeError):
            events = []
        events.append({"at": at, "by": "owner", "enabled": enabled})
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.voice_path.write_text(json.dumps({"enabled": enabled, "events": events}, indent=1) + "\n", encoding="utf-8")
        return ("My voice is back on." if enabled else
                "My voice is off: I'll answer plainly, without personality, until you say !voice on.")

    def describe_history(self) -> str:
        lines = ["My personality history (every change is a new version; nothing is erased):"]
        for p in self.history():
            levels = ", ".join(f"{t.name} {t.level}" for t in p.traits)
            lines.append(f"v{p.version} {p.created_at[:16]} {p.provenance[0]}: {levels}")
        lines.append(f"Voice is {'on' if self.enabled() else 'off'}. !personality restore N brings back version N.")
        return "\n".join(lines)
