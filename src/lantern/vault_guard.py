"""Fail closed when Prime's encrypted vault is not mounted.

Prime's private state lives under the vault mount (default `/srv/lantern`). If
the vault is closed that directory is an ordinary folder on the unencrypted
disk, and opening a store there would silently create a new, unprotected
database (this happened once on the Tablet, 2026-10-22). Every store that
opens a path inside the vault root checks the mount first.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

DEFAULT_VAULT_ROOT = Path("/srv/lantern")


class VaultClosedError(RuntimeError):
    """A store path lies inside the vault root while the vault is not mounted."""


def vault_root() -> Path:
    return Path(os.environ.get("LANTERN_VAULT_MOUNT", str(DEFAULT_VAULT_ROOT)))


def require_vault_open(
    path: str | Path,
    *,
    root: str | Path | None = None,
    ismount: Callable[[str], bool] = os.path.ismount,
) -> None:
    """Raise VaultClosedError if `path` is inside the vault root and it is not mounted."""
    base = Path(os.path.abspath(root if root is not None else vault_root()))
    target = Path(os.path.abspath(path))
    if target != base and base not in target.parents:
        return
    if not ismount(str(base)):
        raise VaultClosedError(
            f"Prime's vault is not open: {base} is not mounted, so nothing was "
            f"read or created at {target}. Open it first: bash scripts/host_vault.sh open"
        )
