"""Versioned, inspectable personality profiles with owner-controlled history."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import hmac
import json
import re
from typing import Iterable

FORMAT = "lantern_personality_profile_v2"
LEGACY_FORMAT = "lantern_personality_profile_v1"
ALLOWED_STATUS = {"draft", "accepted", "disabled"}
ALLOWED_TRAITS = {
    "curiosity", "directness", "explanation_detail", "humor",
    "playfulness", "social_energy", "warmth",
}
_ID = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
_REF = re.compile(r"[a-z][a-z0-9_.:/-]{2,159}")
_HEX = re.compile(r"[0-9a-f]{64}")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("personality timestamps require an offset")
    return parsed


@dataclass(frozen=True, slots=True)
class PersonalityTrait:
    name: str
    level: int
    provenance: str

    def validate(self) -> None:
        if self.name not in ALLOWED_TRAITS:
            raise ValueError("unknown personality trait")
        if type(self.level) is not int or not 0 <= self.level <= 5:
            raise ValueError("personality trait level must be zero through five")
        if not _REF.fullmatch(self.provenance):
            raise ValueError("invalid personality trait provenance")


@dataclass(frozen=True, slots=True)
class PersonalityProfile:
    profile_id: str
    instance_id: str
    owner_id: str
    version: int
    status: str
    created_at: str
    traits: tuple[PersonalityTrait, ...]
    boundaries: tuple[str, ...]
    provenance: tuple[str, ...]
    previous_profile_sha256: str | None
    knowledge_authority: bool
    permission_authority: bool
    safety_authority: bool
    profile_sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "boundaries": list(self.boundaries),
            "created_at": self.created_at,
            "format": FORMAT,
            "instance_id": self.instance_id,
            "knowledge_authority": self.knowledge_authority,
            "owner_id": self.owner_id,
            "permission_authority": self.permission_authority,
            "previous_profile_sha256": self.previous_profile_sha256,
            "profile_id": self.profile_id,
            "provenance": list(self.provenance),
            "safety_authority": self.safety_authority,
            "status": self.status,
            "traits": [asdict(trait) for trait in self.traits],
            "version": self.version,
        }

    def validate(self) -> "PersonalityProfile":
        if not _ID.fullmatch(self.profile_id):
            raise ValueError("invalid personality profile identifier")
        if not _ID.fullmatch(self.instance_id) or not _ID.fullmatch(self.owner_id):
            raise ValueError("personality profile requires instance and owner identities")
        if type(self.version) is not int or not 1 <= self.version <= 1_000_000:
            raise ValueError("invalid personality profile version")
        if self.status not in ALLOWED_STATUS:
            raise ValueError("invalid personality profile status")
        _aware(self.created_at)
        if not 1 <= len(self.traits) <= len(ALLOWED_TRAITS):
            raise ValueError("personality profile requires bounded traits")
        names = [trait.name for trait in self.traits]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("personality traits must be unique and canonical")
        for trait in self.traits:
            trait.validate()
        if not 1 <= len(self.boundaries) <= 32:
            raise ValueError("personality boundaries required")
        if any(
            not isinstance(item, str) or not item.strip() or len(item) > 240
            for item in self.boundaries
        ) or len(set(self.boundaries)) != len(self.boundaries):
            raise ValueError("invalid personality boundary")
        if not 1 <= len(self.provenance) <= 32:
            raise ValueError("personality provenance required")
        if any(not _REF.fullmatch(item) for item in self.provenance):
            raise ValueError("invalid personality provenance")
        if self.previous_profile_sha256 is not None and not _HEX.fullmatch(
            self.previous_profile_sha256
        ):
            raise ValueError("invalid previous personality digest")
        if self.version == 1 and self.previous_profile_sha256 is not None:
            raise ValueError("initial personality profile cannot have a predecessor")
        if self.version > 1 and self.previous_profile_sha256 is None:
            raise ValueError("personality revision requires a predecessor")
        if self.knowledge_authority or self.permission_authority or self.safety_authority:
            raise PermissionError("personality cannot grant knowledge, permission, or safety authority")
        expected = hashlib.sha256(_canonical(self.payload())).hexdigest()
        if not hmac.compare_digest(expected, self.profile_sha256):
            raise ValueError("personality profile digest mismatch")
        if len(_canonical({**self.payload(), "profile_sha256": self.profile_sha256})) > 32 * 1024:
            raise ValueError("personality profile exceeds size limit")
        return self


def make_profile(
    *,
    profile_id: str,
    instance_id: str,
    owner_id: str,
    version: int,
    status: str,
    created_at: str,
    traits: Iterable[PersonalityTrait],
    boundaries: Iterable[str],
    provenance: Iterable[str],
    previous_profile_sha256: str | None = None,
) -> PersonalityProfile:
    ordered_traits = tuple(sorted(tuple(traits), key=lambda item: item.name))
    provisional = PersonalityProfile(
        profile_id=profile_id,
        instance_id=instance_id,
        owner_id=owner_id,
        version=version,
        status=status,
        created_at=created_at,
        traits=ordered_traits,
        boundaries=tuple(boundaries),
        provenance=tuple(provenance),
        previous_profile_sha256=previous_profile_sha256,
        knowledge_authority=False,
        permission_authority=False,
        safety_authority=False,
        profile_sha256="",
    )
    digest = hashlib.sha256(_canonical(provisional.payload())).hexdigest()
    return replace(provisional, profile_sha256=digest).validate()


@dataclass(frozen=True, slots=True)
class PersonalityRevisionProposal:
    proposal_id: str
    base_profile_sha256: str
    candidate: PersonalityProfile
    reason: str
    evidence_references: tuple[str, ...]
    applied: bool = False

    def validate(self) -> "PersonalityRevisionProposal":
        if not _ID.fullmatch(self.proposal_id):
            raise ValueError("invalid personality proposal identifier")
        if not _HEX.fullmatch(self.base_profile_sha256):
            raise ValueError("invalid personality proposal base")
        self.candidate.validate()
        if self.candidate.previous_profile_sha256 != self.base_profile_sha256:
            raise ValueError("personality proposal does not extend its base")
        if self.candidate.status != "draft" or self.applied:
            raise PermissionError("personality proposals must remain unapplied drafts")
        if not self.reason.strip() or len(self.reason) > 1000:
            raise ValueError("personality proposal requires a bounded reason")
        if not self.evidence_references or any(
            not _REF.fullmatch(item) for item in self.evidence_references
        ):
            raise ValueError("personality proposal requires valid evidence")
        return self


class PersonalityRegistry:
    """One Lantern instance's personality history, changeable only by its owner."""

    def __init__(self, *, instance_id: str, owner_id: str) -> None:
        if not _ID.fullmatch(instance_id) or not _ID.fullmatch(owner_id):
            raise ValueError("personality registry requires instance and owner identities")
        self.instance_id = instance_id
        self.owner_id = owner_id
        self._profiles: dict[str, PersonalityProfile] = {}
        self._active: str | None = None

    def _require_owner(self, actor: str) -> None:
        if not hmac.compare_digest(actor.encode("utf-8"), self.owner_id.encode("utf-8")):
            raise PermissionError("only this instance's owner can change its personality")

    def _require_bound(self, profile: PersonalityProfile) -> None:
        if profile.instance_id != self.instance_id:
            raise ValueError("personality profile belongs to a different instance")
        if profile.owner_id != self.owner_id:
            raise PermissionError("personality profile names a different owner")

    @property
    def active(self) -> PersonalityProfile | None:
        return None if self._active is None else self._profiles[self._active]

    def accept(self, profile: PersonalityProfile, *, actor: str) -> None:
        self._require_owner(actor)
        profile.validate()
        self._require_bound(profile)
        if profile.status != "accepted":
            raise ValueError("only accepted personality profiles can become active")
        current = self.active
        if current is None:
            if profile.version != 1 or profile.previous_profile_sha256 is not None:
                raise ValueError("personality history must begin at version one")
        elif (
            profile.version != current.version + 1
            or profile.previous_profile_sha256 != current.profile_sha256
        ):
            raise ValueError("personality revision must extend the active profile")
        if profile.profile_sha256 in self._profiles:
            raise ValueError("personality profile already recorded")
        self._profiles[profile.profile_sha256] = profile
        self._active = profile.profile_sha256

    def rollback(self, profile_sha256: str, *, actor: str) -> PersonalityProfile:
        self._require_owner(actor)
        try:
            profile = self._profiles[profile_sha256]
        except KeyError as error:
            raise ValueError("unknown personality rollback target") from error
        self._active = profile_sha256
        return profile

    def history(self) -> tuple[PersonalityProfile, ...]:
        return tuple(sorted(self._profiles.values(), key=lambda item: item.version))
