"""File staging for signal-cli attachment delivery.

The hermes-agent and signal-cli-rest-api containers share the ``hermes-data``
volume mounted at ``/opt/data``. signal-cli can only open files that live under
that shared root, so any attachment created elsewhere (workspace dirs, /tmp, …)
must be copied into the shared document cache before the RPC ``send``.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger("hermes.plugins.signal_group_chat.staging")

# Root of the shared Docker volume.
SHARED_ROOT = Path("/opt/data")


def stage_for_signal(file_path: str) -> str:
    """Ensure *file_path* is reachable from the signal-cli container.

    - Files already under ``/opt/data`` are returned unchanged.
    - Files elsewhere are copied to ``/opt/data/cache/documents/staged_<uuid>_<name>``.
    - Missing files are returned as-is so the caller's FileNotFoundError handler
      produces the right error message.
    """
    src = Path(file_path).resolve()
    try:
        shared_root = SHARED_ROOT.resolve()
        try:
            inside = src.is_relative_to(shared_root)
        except AttributeError:  # pragma: no cover — Python <3.9
            inside = str(src).startswith(str(shared_root) + "/")
        if inside:
            return str(src)
    except Exception:
        return file_path

    if not src.exists():
        return file_path

    cache_dir = SHARED_ROOT / "cache" / "documents"
    cache_dir.mkdir(parents=True, exist_ok=True)
    staged_path = cache_dir / f"staged_{uuid.uuid4().hex[:12]}_{src.name}"
    try:
        shutil.copy2(str(src), str(staged_path))
        logger.debug("Signal: staged %s -> %s for signal-cli", file_path, staged_path)
        return str(staged_path)
    except Exception as exc:
        logger.warning("Signal: could not stage %s for signal-cli: %s", file_path, exc)
        return file_path
