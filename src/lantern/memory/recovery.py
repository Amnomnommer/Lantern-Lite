"""Verified backup and isolated restore for Lantern's durable memory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from lantern.memory.store import MemoryCorruptionError, MemoryStore


_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_version: int
    backup_id: str
    created_at: str
    schema_version: int
    state_digest: str
    database_sha256: str
    integrity_snapshot_id: str
    raw_staging_included: bool
    encryption_boundary_confirmed: bool


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    database_path: Path
    manifest_path: Path
    manifest: BackupManifest


class BackupVerificationError(RuntimeError):
    """A backup or manifest failed validation and cannot be restored."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema_version(path: Path) -> int:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        if row is None:
            raise BackupVerificationError("backup has no schema version")
        return int(row[0])
    except sqlite3.DatabaseError as error:
        raise BackupVerificationError(
            f"schema inspection failed: {type(error).__name__}"
        ) from error
    finally:
        connection.close()


def _write_manifest(path: Path, manifest: BackupManifest) -> None:
    payload = json.dumps(
        asdict(manifest),
        sort_keys=True,
        separators=(",", ":"),
    )
    with path.open("x", encoding="utf-8") as destination:
        destination.write(payload)
        destination.write("\n")
        destination.flush()
        os.fsync(destination.fileno())
    path.chmod(0o600)


def load_manifest(path: str | Path) -> BackupManifest:
    manifest_path = Path(path)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = BackupManifest(**data)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BackupVerificationError("backup manifest is invalid") from error
    if manifest.format_version != 1:
        raise BackupVerificationError("unsupported backup manifest version")
    if manifest.raw_staging_included:
        raise BackupVerificationError("raw staging must not enter backups")
    if not manifest.encryption_boundary_confirmed:
        raise BackupVerificationError("backup lacks an encryption boundary")
    return manifest


def verify_backup(
    database_path: str | Path,
    manifest_path: str | Path,
) -> BackupManifest:
    database = Path(database_path)
    manifest = load_manifest(manifest_path)
    if _file_sha256(database) != manifest.database_sha256:
        raise BackupVerificationError("backup file hash does not match manifest")

    report = MemoryStore.inspect_database(database)
    if not report.healthy:
        raise BackupVerificationError(
            "backup database failed SQLite integrity validation"
        )
    try:
        state_digest = MemoryStore.digest_database(database)
    except MemoryCorruptionError as error:
        raise BackupVerificationError(
            "backup state digest could not be verified"
        ) from error
    if state_digest != manifest.state_digest:
        raise BackupVerificationError("backup state digest does not match")
    if _schema_version(database) != manifest.schema_version:
        raise BackupVerificationError("backup schema version does not match")
    return manifest


def create_verified_backup(
    memory: MemoryStore,
    destination_directory: str | Path,
    label: str,
    *,
    encryption_boundary_confirmed: bool,
) -> BackupArtifact:
    """Create a verified snapshot only inside a confirmed encrypted boundary."""

    if not encryption_boundary_confirmed:
        raise PermissionError(
            "private backup creation requires a confirmed encryption boundary"
        )
    if _SAFE_LABEL.fullmatch(label) is None:
        raise ValueError("backup label must be filesystem-safe")

    directory = Path(destination_directory)
    directory.mkdir(parents=True, exist_ok=True)
    database_path = directory / f"{label}.sqlite3"
    manifest_path = directory / f"{label}.manifest.json"
    if database_path.exists() or manifest_path.exists():
        raise FileExistsError("backup label already exists")

    nonce = uuid.uuid4().hex
    temporary_database = directory / f".{label}.{nonce}.tmp.sqlite3"
    temporary_manifest = directory / f".{label}.{nonce}.tmp.manifest"
    snapshot = memory.record_integrity_snapshot("pre_backup")

    try:
        memory.copy_to(temporary_database)
        temporary_database.chmod(0o600)
        report = MemoryStore.inspect_database(temporary_database)
        if not report.healthy:
            raise BackupVerificationError(
                "new backup failed SQLite integrity validation"
            )
        if (
            MemoryStore.digest_database(temporary_database)
            != snapshot.state_digest
        ):
            raise BackupVerificationError(
                "new backup does not match authoritative state"
            )

        manifest = BackupManifest(
            format_version=1,
            backup_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            ),
            schema_version=memory.schema_version,
            state_digest=snapshot.state_digest,
            database_sha256=_file_sha256(temporary_database),
            integrity_snapshot_id=snapshot.id,
            raw_staging_included=False,
            encryption_boundary_confirmed=True,
        )
        _write_manifest(temporary_manifest, manifest)
        os.replace(temporary_database, database_path)
        os.replace(temporary_manifest, manifest_path)
        verify_backup(database_path, manifest_path)
        return BackupArtifact(database_path, manifest_path, manifest)
    finally:
        temporary_database.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)


def restore_verified_backup(
    database_path: str | Path,
    manifest_path: str | Path,
    destination_path: str | Path,
) -> BackupManifest:
    """Restore into a new isolated path; never overwrite authoritative state."""

    source_path = Path(database_path)
    manifest = verify_backup(source_path, manifest_path)
    destination = Path(destination_path)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.restore"
    )

    source_uri = source_path.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=5.0)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    try:
        if not MemoryStore.inspect_database(temporary).healthy:
            raise BackupVerificationError(
                "restored database failed SQLite integrity validation"
            )
        if MemoryStore.digest_database(temporary) != manifest.state_digest:
            raise BackupVerificationError(
                "restored state does not match the backup manifest"
            )
        if _schema_version(temporary) != manifest.schema_version:
            raise BackupVerificationError(
                "restored schema does not match the backup manifest"
            )
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        with MemoryStore(destination) as restored:
            if restored.state_digest() != manifest.state_digest:
                raise BackupVerificationError(
                    "activated restore changed authoritative state"
                )
            restored.record_integrity_snapshot("post_restore")
        return manifest
    except BaseException:
        temporary.unlink(missing_ok=True)
        if destination.exists():
            destination.unlink()
        raise
