"""Offline trusted-user, device enrollment, and attributed creative-session contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import hmac
import json
import re
from typing import Iterable

INVITATION_FORMAT = "lantern_trusted_invitation_v1"
DEVICE_CLAIM_FORMAT = "lantern_trusted_device_claim_v1"
SESSION_FORMAT = "lantern_creative_session_v1"
CONTRIBUTION_FORMAT = "lantern_creative_contribution_v1"
INITIAL_CAPABILITY = "creative.session.participate"
ALLOWED_MODES = {"idea", "continuation", "critique", "synthesis", "revision"}
ALLOWED_RETENTION = {"transient", "reviewed_export"}
_ID = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
_REF = re.compile(r"[a-z][a-z0-9_.:/-]{2,159}")
_HEX = re.compile(r"[0-9a-f]{64}")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("trusted collaboration timestamps require an offset")
    return parsed


def _bounded_text(value: str, field: str, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be nonempty and bounded")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError(f"{field} contains control characters")
    return value


@dataclass(frozen=True, slots=True)
class TrustedUserInvitation:
    invitation_id: str
    user_id: str
    display_label: str
    issued_at: str
    expires_at: str
    capabilities: tuple[str, ...]
    maximum_devices: int
    owner_approved: bool
    invitation_sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "capabilities": list(self.capabilities),
            "display_label": self.display_label,
            "expires_at": self.expires_at,
            "format": INVITATION_FORMAT,
            "invitation_id": self.invitation_id,
            "issued_at": self.issued_at,
            "maximum_devices": self.maximum_devices,
            "owner_approved": self.owner_approved,
            "user_id": self.user_id,
        }

    def validate(self, *, now: str | None = None) -> "TrustedUserInvitation":
        if not _ID.fullmatch(self.invitation_id) or not _ID.fullmatch(self.user_id):
            raise ValueError("invalid trusted invitation identity")
        _bounded_text(self.display_label, "display label", 80)
        issued, expires = _aware(self.issued_at), _aware(self.expires_at)
        if expires <= issued:
            raise ValueError("trusted invitation expiry must follow issuance")
        if now is not None and not issued <= _aware(now) < expires:
            raise PermissionError("trusted invitation is not active")
        if self.capabilities != (INITIAL_CAPABILITY,):
            raise PermissionError("initial invitation capability ceiling exceeded")
        if type(self.maximum_devices) is not int or self.maximum_devices != 1:
            raise PermissionError("initial invitation permits exactly one device")
        if not self.owner_approved:
            raise PermissionError("trusted invitation requires owner approval")
        if not hmac.compare_digest(_digest(self.payload()), self.invitation_sha256):
            raise ValueError("trusted invitation digest mismatch")
        return self


def make_invitation(
    *,
    invitation_id: str,
    user_id: str,
    display_label: str,
    issued_at: str,
    expires_at: str,
    capabilities: Iterable[str] = (INITIAL_CAPABILITY,),
    maximum_devices: int = 1,
    owner_approved: bool,
) -> TrustedUserInvitation:
    provisional = TrustedUserInvitation(
        invitation_id, user_id, display_label, issued_at, expires_at,
        tuple(capabilities), maximum_devices, owner_approved, "",
    )
    return replace(
        provisional, invitation_sha256=_digest(provisional.payload())
    ).validate()


@dataclass(frozen=True, slots=True)
class TrustedDeviceClaim:
    invitation_sha256: str
    user_id: str
    device_id: str
    device_public_key_sha256: str
    client_protocol: str
    created_at: str
    private_key_exported: bool
    claim_sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "client_protocol": self.client_protocol,
            "created_at": self.created_at,
            "device_id": self.device_id,
            "device_public_key_sha256": self.device_public_key_sha256,
            "format": DEVICE_CLAIM_FORMAT,
            "invitation_sha256": self.invitation_sha256,
            "private_key_exported": self.private_key_exported,
            "user_id": self.user_id,
        }

    def validate(self) -> "TrustedDeviceClaim":
        if not _HEX.fullmatch(self.invitation_sha256):
            raise ValueError("invalid claim invitation digest")
        if not _ID.fullmatch(self.user_id) or not _ID.fullmatch(self.device_id):
            raise ValueError("invalid trusted device identity")
        if not _HEX.fullmatch(self.device_public_key_sha256):
            raise ValueError("invalid trusted device public-key fingerprint")
        if self.client_protocol != "lantern-collaborator/0.1":
            raise ValueError("unsupported collaborator protocol")
        _aware(self.created_at)
        if self.private_key_exported:
            raise PermissionError("device private key must remain local")
        if not hmac.compare_digest(_digest(self.payload()), self.claim_sha256):
            raise ValueError("trusted device claim digest mismatch")
        return self


def make_device_claim(
    *,
    invitation_sha256: str,
    user_id: str,
    device_id: str,
    device_public_key_sha256: str,
    created_at: str,
    client_protocol: str = "lantern-collaborator/0.1",
) -> TrustedDeviceClaim:
    provisional = TrustedDeviceClaim(
        invitation_sha256, user_id, device_id, device_public_key_sha256,
        client_protocol, created_at, False, "",
    )
    return replace(provisional, claim_sha256=_digest(provisional.payload())).validate()


@dataclass(frozen=True, slots=True)
class TrustedUserRecord:
    user_id: str
    display_label: str
    device_id: str
    device_public_key_sha256: str
    capabilities: tuple[str, ...]
    invitation_sha256: str
    claim_sha256: str
    status: str = "active"


class TrustedEnrollmentRegistry:
    """One-shot in-memory enrollment contract with explicit owner revocation."""

    def __init__(self) -> None:
        self._consumed: set[str] = set()
        self._users: dict[str, TrustedUserRecord] = {}

    def enroll(
        self,
        invitation: TrustedUserInvitation,
        claim: TrustedDeviceClaim,
        *,
        actor: str,
        now: str,
    ) -> TrustedUserRecord:
        if actor != "owner":
            raise PermissionError("only the owner can approve trusted enrollment")
        invitation.validate(now=now)
        claim.validate()
        if invitation.invitation_sha256 in self._consumed:
            raise PermissionError("trusted invitation already consumed")
        if claim.invitation_sha256 != invitation.invitation_sha256:
            raise PermissionError("device claim is not bound to this invitation")
        if claim.user_id != invitation.user_id:
            raise PermissionError("device claim user does not match invitation")
        if invitation.user_id in self._users:
            raise PermissionError("trusted user already enrolled")
        record = TrustedUserRecord(
            invitation.user_id, invitation.display_label, claim.device_id,
            claim.device_public_key_sha256, invitation.capabilities,
            invitation.invitation_sha256, claim.claim_sha256,
        )
        self._consumed.add(invitation.invitation_sha256)
        self._users[record.user_id] = record
        return record

    def persistence_snapshot(self) -> dict[str, object]:
        """Return public enrollment state only; no credential material is present."""
        return {
            "consumed_invitation_sha256": sorted(self._consumed),
            "users": [
                {
                    **asdict(self._users[user_id]),
                    "capabilities": list(self._users[user_id].capabilities),
                }
                for user_id in sorted(self._users)
            ],
        }

    @classmethod
    def from_persistence_snapshot(
        cls, snapshot: dict[str, object],
    ) -> "TrustedEnrollmentRegistry":
        if set(snapshot) != {"consumed_invitation_sha256", "users"}:
            raise ValueError("invalid trusted enrollment snapshot fields")
        consumed = snapshot["consumed_invitation_sha256"]
        users = snapshot["users"]
        if (
            not isinstance(consumed, list)
            or any(not isinstance(item, str) or not _HEX.fullmatch(item) for item in consumed)
            or consumed != sorted(set(consumed))
            or not isinstance(users, list)
        ):
            raise ValueError("invalid trusted enrollment snapshot")
        registry = cls()
        registry._consumed = set(consumed)
        for value in users:
            if not isinstance(value, dict) or set(value) != {
                "capabilities", "claim_sha256", "device_id",
                "device_public_key_sha256", "display_label",
                "invitation_sha256", "status", "user_id",
            }:
                raise ValueError("invalid trusted user record fields")
            record = TrustedUserRecord(
                user_id=value["user_id"],
                display_label=value["display_label"],
                device_id=value["device_id"],
                device_public_key_sha256=value["device_public_key_sha256"],
                capabilities=tuple(value["capabilities"])
                if isinstance(value["capabilities"], list) else (),
                invitation_sha256=value["invitation_sha256"],
                claim_sha256=value["claim_sha256"],
                status=value["status"],
            )
            if (
                not isinstance(record.user_id, str)
                or not _ID.fullmatch(record.user_id)
                or not isinstance(record.device_id, str)
                or not _ID.fullmatch(record.device_id)
                or not isinstance(record.display_label, str)
                or not record.display_label.strip()
                or len(record.display_label) > 80
                or not isinstance(record.device_public_key_sha256, str)
                or not _HEX.fullmatch(record.device_public_key_sha256)
                or not isinstance(record.invitation_sha256, str)
                or not _HEX.fullmatch(record.invitation_sha256)
                or not isinstance(record.claim_sha256, str)
                or not _HEX.fullmatch(record.claim_sha256)
                or record.capabilities != (INITIAL_CAPABILITY,)
                or record.status not in {"active", "paused", "revoked"}
                or record.invitation_sha256 not in registry._consumed
                or record.user_id in registry._users
            ):
                raise ValueError("invalid trusted user record")
            registry._users[record.user_id] = record
        return registry

    def require(self, user_id: str, capability: str) -> TrustedUserRecord:
        record = self._users.get(user_id)
        if record is None or record.status != "active" or capability not in record.capabilities:
            raise PermissionError("trusted user capability unavailable")
        return record

    def pause(self, user_id: str, *, actor: str) -> TrustedUserRecord:
        return self._set_status(user_id, "paused", actor)

    def revoke(self, user_id: str, *, actor: str) -> TrustedUserRecord:
        return self._set_status(user_id, "revoked", actor)

    def _set_status(self, user_id: str, status: str, actor: str) -> TrustedUserRecord:
        if actor != "owner":
            raise PermissionError("only the owner can change trusted-user status")
        try:
            current = self._users[user_id]
        except KeyError as error:
            raise ValueError("unknown trusted user") from error
        updated = replace(current, status=status)
        self._users[user_id] = updated
        return updated


@dataclass(frozen=True, slots=True)
class CreativeSession:
    session_id: str
    project_reference: str
    participants: tuple[str, ...]
    allowed_modes: tuple[str, ...]
    shared_context_references: tuple[str, ...]
    retention: str
    created_at: str
    status: str
    private_context_included: bool
    session_sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "allowed_modes": list(self.allowed_modes),
            "created_at": self.created_at,
            "format": SESSION_FORMAT,
            "participants": list(self.participants),
            "private_context_included": self.private_context_included,
            "project_reference": self.project_reference,
            "retention": self.retention,
            "session_id": self.session_id,
            "shared_context_references": list(self.shared_context_references),
            "status": self.status,
        }

    def validate(self) -> "CreativeSession":
        if not _ID.fullmatch(self.session_id) or not _REF.fullmatch(self.project_reference):
            raise ValueError("invalid creative session identity")
        if len(self.participants) != 2 or self.participants[0] != "owner":
            raise ValueError("initial creative session requires owner and one collaborator")
        if len(set(self.participants)) != 2 or not _ID.fullmatch(self.participants[1]):
            raise ValueError("invalid creative session participants")
        if (
            not self.allowed_modes
            or tuple(sorted(set(self.allowed_modes))) != self.allowed_modes
            or any(mode not in ALLOWED_MODES for mode in self.allowed_modes)
        ):
            raise ValueError("invalid creative session modes")
        if not self.shared_context_references or any(
            not _REF.fullmatch(item) or item.startswith("private:")
            for item in self.shared_context_references
        ):
            raise PermissionError("shared context must be explicit and non-private")
        if self.retention not in ALLOWED_RETENTION or self.status not in {
            "open", "paused", "closed",
        }:
            raise ValueError("invalid creative session policy")
        _aware(self.created_at)
        if self.private_context_included:
            raise PermissionError("private context cannot enter a shared creative session")
        if not hmac.compare_digest(_digest(self.payload()), self.session_sha256):
            raise ValueError("creative session digest mismatch")
        return self


def make_creative_session(
    *,
    session_id: str,
    project_reference: str,
    collaborator_user_id: str,
    allowed_modes: Iterable[str],
    shared_context_references: Iterable[str],
    retention: str,
    created_at: str,
) -> CreativeSession:
    provisional = CreativeSession(
        session_id, project_reference, ("owner", collaborator_user_id),
        tuple(sorted(set(allowed_modes))), tuple(shared_context_references),
        retention, created_at, "open", False, "",
    )
    return replace(provisional, session_sha256=_digest(provisional.payload())).validate()


@dataclass(frozen=True, slots=True)
class CreativeContribution:
    contribution_id: str
    session_sha256: str
    participant_id: str
    mode: str
    content: str
    previous_contribution_sha256: str | None
    candidate_only: bool
    contribution_sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "candidate_only": self.candidate_only,
            "content": self.content,
            "contribution_id": self.contribution_id,
            "format": CONTRIBUTION_FORMAT,
            "mode": self.mode,
            "participant_id": self.participant_id,
            "previous_contribution_sha256": self.previous_contribution_sha256,
            "session_sha256": self.session_sha256,
        }

    def validate(
        self, session: CreativeSession, *, expected_previous: str | None,
    ) -> "CreativeContribution":
        session.validate()
        if not _ID.fullmatch(self.contribution_id):
            raise ValueError("invalid creative contribution identity")
        if self.session_sha256 != session.session_sha256:
            raise PermissionError("creative contribution session mismatch")
        if self.participant_id not in session.participants:
            raise PermissionError("creative contribution participant not in session")
        if self.mode not in session.allowed_modes:
            raise PermissionError("creative contribution mode not allowed")
        _bounded_text(self.content, "creative contribution", 16_000)
        if self.previous_contribution_sha256 != expected_previous:
            raise PermissionError("creative contribution chain mismatch")
        if self.previous_contribution_sha256 is not None and not _HEX.fullmatch(
            self.previous_contribution_sha256
        ):
            raise ValueError("invalid previous contribution digest")
        if not self.candidate_only:
            raise PermissionError("shared creative contributions must remain candidates")
        if not hmac.compare_digest(_digest(self.payload()), self.contribution_sha256):
            raise ValueError("creative contribution digest mismatch")
        return self


def make_contribution(
    *,
    session: CreativeSession,
    contribution_id: str,
    participant_id: str,
    mode: str,
    content: str,
    previous_contribution_sha256: str | None,
) -> CreativeContribution:
    provisional = CreativeContribution(
        contribution_id, session.session_sha256, participant_id, mode, content,
        previous_contribution_sha256, True, "",
    )
    return replace(
        provisional, contribution_sha256=_digest(provisional.payload())
    ).validate(session, expected_previous=previous_contribution_sha256)
