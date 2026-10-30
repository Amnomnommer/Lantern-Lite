#!/usr/bin/env bash
# The Lite Lantern in a terminal: one public Lantern for everyone (owner amendment, 2026-10-30; LITE_NODE.md).
# In GitHub Codespaces this starts by itself; anywhere with Python 3.12 and this repository it also works.
# Its memory stays in ~/.lantern-lite; what it read is saved to its public memory space as an issue under your own
# GitHub sign-in (gh), or kept in ~/.lantern-lite/outbox when gh is not signed in. Nothing you say is published.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ -d "$here/src/lantern" ]] || here=$(cd "$here/.." && pwd)  # scripts/ here; the root of Lantern-Lite there
save=()
if ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
    save=(--no-save)
    echo "(gh is not signed in: what I read waits in ~/.lantern-lite/outbox instead of my public memory.)"
fi
PYTHONPATH="$here/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m lantern.lite "${save[@]}" "$@"
