"""Atomic, restart-safe persistence for personality and trusted-user state."""

from __future__ import annotations

from dataclasses import asdict
try:
    import fcntl
except ImportError:  # the Lite Lantern's browser (Pyodide) has no fcntl: one page, one writer
    fcntl = None  # type: ignore[assignment]
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Callable, TypeVar

from lantern.personality_profile import (
    FORMAT as PROFILE_FORMAT,
    LEGACY_FORMAT as LEGACY_PROFILE_FORMAT,
    PersonalityProfile,
    PersonalityRegistry,
    PersonalityTrait,
    make_profile,
)
from lantern.trusted_collaboration import TrustedEnrollmentRegistry

PERSONALITY_STORE_FORMAT = "lantern_personality_store_v2"
LEGACY_PERSONALITY_STORE_FORMAT = "lantern_personality_store_v1"
TRUST_STORE_FORMAT = "lantern_trusted_enrollment_store_v1"
_MAXIMUM_BYTES = 1024 * 1024
_T = TypeVar("_T")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _envelope(format_name: str, payload: dict[str, object]) -> dict[str, object]:
    body = {"format": format_name, "payload": payload}
    return {**body, "store_sha256": hashlib.sha256(_canonical(body)).hexdigest()}


def _validate_envelope(
    value: object, format_name: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "format", "payload", "store_sha256",
    }:
        raise ValueError("invalid durable identity store envelope")
    if value["format"] != format_name or not isinstance(value["payload"], dict):
        raise ValueError("unexpected durable identity store format")
    digest = value["store_sha256"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("invalid durable identity store digest")
    body = {"format": value["format"], "payload": value["payload"]}
    expected = hashlib.sha256(_canonical(body)).hexdigest()
    if not hmac.compare_digest(expected, digest):
        raise ValueError("durable identity store digest mismatch")
    return value["payload"]


class _AtomicJsonStore:
    def __init__(self, path: Path, format_name: str) -> None:
        self.path = Path(path)
        self.format_name = format_name
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _prepare_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink():
            raise PermissionError("identity store parent cannot be a symlink")
        os.chmod(self.path.parent, 0o700)

    def _locked(self, operation: Callable[[], _T]) -> _T:
        self._prepare_parent()
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            if fcntl is None:
                if sys.platform != "emscripten":  # anywhere with other processes, never write unlocked
                    raise RuntimeError("file locking is unavailable here")
                return operation()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return operation()
        finally:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def write(self, payload: dict[str, object]) -> None:
        encoded = _canonical(_envelope(self.format_name, payload)) + b"\n"
        if len(encoded) > _MAXIMUM_BYTES:
            raise ValueError("durable identity store exceeds size limit")

        def operation() -> None:
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                dir=self.path.parent,
            )
            try:
                os.fchmod(temporary_fd, 0o600)
                with os.fdopen(temporary_fd, "wb", closefd=True) as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, self.path)
                os.chmod(self.path, 0o600)
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise

        self._locked(operation)

    def read(self) -> dict[str, object]:
        def operation() -> dict[str, object]:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise PermissionError("identity store must be a regular file")
                if stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise PermissionError("identity store mode must be 0600")
                encoded = os.read(descriptor, _MAXIMUM_BYTES + 1)
            finally:
                os.close(descriptor)
            if len(encoded) > _MAXIMUM_BYTES:
                raise ValueError("durable identity store exceeds size limit")
            try:
                decoded = json.loads(encoded.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("invalid durable identity store JSON") from error
            return _validate_envelope(decoded, self.format_name)

        return self._locked(operation)


def _profile_value(profile: PersonalityProfile) -> dict[str, object]:
    return {**profile.payload(), "profile_sha256": profile.profile_sha256}


_PROFILE_FIELDS = {
    "boundaries", "created_at", "format", "instance_id", "knowledge_authority",
    "owner_id", "permission_authority", "previous_profile_sha256",
    "profile_id", "profile_sha256", "provenance", "safety_authority",
    "status", "traits", "version",
}
_LEGACY_PROFILE_FIELDS = _PROFILE_FIELDS - {"instance_id", "owner_id"}


def _parse_traits(value: object) -> tuple[PersonalityTrait, ...]:
    if not isinstance(value, list):
        raise ValueError("invalid persisted personality traits")
    parsed = []
    for trait in value:
        if not isinstance(trait, dict) or set(trait) != {
            "level", "name", "provenance",
        }:
            raise ValueError("invalid persisted personality trait")
        parsed.append(PersonalityTrait(**trait))
    return tuple(parsed)


def _parse_collections(value: dict[str, object]) -> None:
    if not isinstance(value["boundaries"], list) or not isinstance(
        value["provenance"], list
    ):
        raise ValueError("invalid persisted personality collections")


def _profile_from_value(value: object) -> PersonalityProfile:
    if not isinstance(value, dict) or set(value) != _PROFILE_FIELDS:
        raise ValueError("invalid persisted personality profile fields")
    if value["format"] != PROFILE_FORMAT:
        raise ValueError("invalid persisted personality profile format")
    _parse_collections(value)
    return PersonalityProfile(
        profile_id=value["profile_id"],
        instance_id=value["instance_id"],
        owner_id=value["owner_id"],
        version=value["version"],
        status=value["status"],
        created_at=value["created_at"],
        traits=_parse_traits(value["traits"]),
        boundaries=tuple(value["boundaries"]),
        provenance=tuple(value["provenance"]),
        previous_profile_sha256=value["previous_profile_sha256"],
        knowledge_authority=value["knowledge_authority"],
        permission_authority=value["permission_authority"],
        safety_authority=value["safety_authority"],
        profile_sha256=value["profile_sha256"],
    ).validate()


def _legacy_profile_digest(value: object) -> str:
    """Verify one v1 profile exactly as v1 computed its digest."""
    if not isinstance(value, dict) or set(value) != _LEGACY_PROFILE_FIELDS:
        raise ValueError("invalid legacy personality profile fields")
    if value["format"] != LEGACY_PROFILE_FORMAT:
        raise ValueError("invalid legacy personality profile format")
    if any(value[name] is not False for name in (
        "knowledge_authority", "permission_authority", "safety_authority",
    )):
        raise PermissionError("legacy personality profile claims authority")
    body = {key: item for key, item in value.items() if key != "profile_sha256"}
    expected = hashlib.sha256(_canonical(body)).hexdigest()
    digest = value["profile_sha256"]
    if not isinstance(digest, str) or not hmac.compare_digest(expected, digest):
        raise ValueError("legacy personality profile digest mismatch")
    return digest


class DurablePersonalityStore:
    """One instance's personality history; refuses any other instance's data."""

    def __init__(self, path: Path, *, instance_id: str, owner_id: str) -> None:
        self._store = _AtomicJsonStore(path, PERSONALITY_STORE_FORMAT)
        PersonalityRegistry(instance_id=instance_id, owner_id=owner_id)
        self.instance_id = instance_id
        self.owner_id = owner_id

    def save(self, registry: PersonalityRegistry) -> None:
        if (registry.instance_id, registry.owner_id) != (
            self.instance_id, self.owner_id,
        ):
            raise ValueError("personality registry belongs to a different instance")
        active = registry.active
        self._store.write({
            "active_profile_sha256": (
                None if active is None else active.profile_sha256
            ),
            "instance_id": self.instance_id,
            "owner_id": self.owner_id,
            "profiles": [_profile_value(item) for item in registry.history()],
        })

    def load(self) -> PersonalityRegistry:
        if self._legacy_payload() is not None:
            raise ValueError("legacy v1 personality store; owner migration required")
        payload = self._store.read()
        if set(payload) != {
            "active_profile_sha256", "instance_id", "owner_id", "profiles",
        }:
            raise ValueError("invalid personality store fields")
        if (payload["instance_id"], payload["owner_id"]) != (
            self.instance_id, self.owner_id,
        ):
            raise ValueError("personality store belongs to a different instance")
        values = payload["profiles"]
        active = payload["active_profile_sha256"]
        if not isinstance(values, list) or not values:
            raise ValueError("personality store requires history")
        profiles = [_profile_from_value(item) for item in values]
        if [item.version for item in profiles] != list(range(1, len(profiles) + 1)):
            raise ValueError("personality store history is not contiguous")
        registry = PersonalityRegistry(
            instance_id=self.instance_id, owner_id=self.owner_id,
        )
        for profile in profiles:
            registry.accept(profile, actor=self.owner_id)
        if not isinstance(active, str):
            raise ValueError("personality store requires an active profile")
        registry.rollback(active, actor=self.owner_id)
        return registry

    def _legacy_payload(self) -> dict[str, object] | None:
        if not self._store.path.exists():
            return None
        legacy = _AtomicJsonStore(self._store.path, LEGACY_PERSONALITY_STORE_FORMAT)
        try:
            return legacy.read()
        except ValueError as error:
            if "unexpected durable identity store format" in str(error):
                return None
            raise

    def migrate_legacy(self, *, actor: str) -> PersonalityRegistry:
        """Owner-run, one-time move of verified v1 history into this instance.

        Each profile keeps its content and timestamps, gains this instance's
        binding and a `migrated:v1:<digest>` provenance entry, and is re-chained.
        The v1 file is kept beside the store as `<name>.v1-backup`.
        """
        PersonalityRegistry(
            instance_id=self.instance_id, owner_id=self.owner_id,
        )._require_owner(actor)
        payload = self._legacy_payload()
        if payload is None:
            raise ValueError("no legacy v1 personality store to migrate")
        if set(payload) != {"active_profile_sha256", "profiles"}:
            raise ValueError("invalid legacy personality store fields")
        values = payload["profiles"]
        if not isinstance(values, list) or not values:
            raise ValueError("legacy personality store requires history")
        registry = PersonalityRegistry(
            instance_id=self.instance_id, owner_id=self.owner_id,
        )
        mapping: dict[str, str] = {}
        previous: str | None = None
        for expected_version, value in enumerate(values, start=1):
            legacy_digest = _legacy_profile_digest(value)
            if value["version"] != expected_version:
                raise ValueError("legacy personality history is not contiguous")
            _parse_collections(value)
            profile = make_profile(
                profile_id=value["profile_id"],
                instance_id=self.instance_id,
                owner_id=self.owner_id,
                version=value["version"],
                status=value["status"],
                created_at=value["created_at"],
                traits=_parse_traits(value["traits"]),
                boundaries=value["boundaries"],
                provenance=(*value["provenance"], f"migrated:v1:{legacy_digest}"),
                previous_profile_sha256=previous,
            )
            registry.accept(profile, actor=actor)
            mapping[legacy_digest] = profile.profile_sha256
            previous = profile.profile_sha256
        active = payload["active_profile_sha256"]
        if not isinstance(active, str) or active not in mapping:
            raise ValueError("legacy personality store requires a known active profile")
        registry.rollback(mapping[active], actor=actor)
        backup = self._store.path.with_name(self._store.path.name + ".v1-backup")
        if backup.exists() or backup.is_symlink():
            raise FileExistsError("legacy personality backup already exists")
        os.link(self._store.path, backup)
        self.save(registry)
        return registry


class DurableTrustedEnrollmentStore:
    def __init__(self, path: Path) -> None:
        self._store = _AtomicJsonStore(path, TRUST_STORE_FORMAT)

    def save(self, registry: TrustedEnrollmentRegistry) -> None:
        self._store.write(registry.persistence_snapshot())

    def load(self) -> TrustedEnrollmentRegistry:
        return TrustedEnrollmentRegistry.from_persistence_snapshot(
            self._store.read()
        )
