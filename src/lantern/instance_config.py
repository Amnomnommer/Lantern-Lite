"""Per-instance configuration: who this Lantern is, who owns it, where it lives.

Node specifics belong here rather than in core code (PERSONALITY_CORE.md,
"Core and instance"). The file holds identifiers and paths only, never secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat

FORMAT = "lantern_instance_config_v1"
_ID = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
_FIELDS = {"format", "instance_id", "owner_display_name", "owner_id", "personality_store"}
_MAXIMUM_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    instance_id: str
    owner_id: str
    owner_display_name: str
    personality_store: Path

    def validate(self) -> "InstanceConfig":
        if not _ID.fullmatch(self.instance_id) or not _ID.fullmatch(self.owner_id):
            raise ValueError("instance config requires instance and owner identities")
        name = self.owner_display_name
        if not name.strip() or len(name) > 64 or any(ch in name for ch in "\r\n\t"):
            raise ValueError("invalid owner display name")
        if not self.personality_store.is_absolute():
            raise ValueError("personality store path must be absolute")
        return self


def load_instance_config(path: str | Path) -> InstanceConfig:
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PermissionError("instance config must be a regular file")
        encoded = os.read(descriptor, _MAXIMUM_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(encoded) > _MAXIMUM_BYTES:
        raise ValueError("instance config exceeds size limit")
    value = json.loads(encoded.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != _FIELDS or value["format"] != FORMAT:
        raise ValueError("invalid instance config fields")
    if any(not isinstance(value[name], str) for name in _FIELDS):
        raise ValueError("instance config values must be strings")
    return InstanceConfig(
        instance_id=value["instance_id"],
        owner_id=value["owner_id"],
        owner_display_name=value["owner_display_name"],
        personality_store=Path(value["personality_store"]),
    ).validate()
