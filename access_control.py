"""Owner/guest access control, group allowlist, and group creation.

A mixin over the upstream SignalAdapter. All persistent grant state goes through
the backup-aware ConfigStore (``self._store``); env vars remain the bootstrap
defaults (owner identity, base group allowlist).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ._util import _parse_comma_list
from .observability import audit, logger

# Sandbox-safe fallback mirror (always a real FS even inside the terminal sandbox).
_TMP_GROUPS = Path("/tmp/hermes_signal_groups.json")


class AccessControlMixin:
    # These attributes are provided by the adapter / upstream base:
    #   self._store, self.group_allow_from, self.account, self._account_normalized
    #   self._rpc(...)

    # -- owner / approval checks --------------------------------------------

    def _is_owner(self, sender: str) -> bool:
        owner_str = os.getenv("SIGNAL_OWNER_USERS", "") or self._account_normalized
        owners = set(_parse_comma_list(owner_str)) | {self._account_normalized}
        return sender in owners

    def _is_approved(self, sender: str, group_id: str | None = None) -> bool:
        if self._is_owner(sender):
            return True
        return self._store.is_approved(sender, group_id)

    # -- group allowlist -----------------------------------------------------

    def _effective_group_allowlist(self) -> set:
        """Static env allowlist + runtime dynamic groups (store + /tmp mirror)."""
        groups = set(self.group_allow_from)
        try:
            groups.update(self._store.dynamic_groups())
        except Exception:
            pass
        try:
            if _TMP_GROUPS.exists():
                data = json.loads(_TMP_GROUPS.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    groups.update(str(g) for g in data if g)
        except Exception:
            pass
        return groups

    # -- system messaging ----------------------------------------------------

    async def _send_group_message(self, group_id: str, text: str) -> None:
        try:
            await self._rpc("send", {"account": self.account, "message": text, "groupId": group_id})
        except Exception as e:
            logger.warning("Signal: failed to send system notice to group %s: %s", group_id[:8], e)

    # -- group creation ------------------------------------------------------

    async def create_group(self, name: str, members: list[str] | None = None) -> str | None:
        """Create a Signal group via signal-cli and add it to the dynamic allowlist."""
        if not getattr(self, "client", None) or not self.account:
            logger.error("Signal: cannot create group - adapter not connected")
            return None

        params: dict[str, Any] = {"account": self.account, "name": name}
        if members:
            params["members"] = members

        group_id = None
        for method in ("createGroup", "updateGroup"):
            result = await self._rpc(method, params)
            if isinstance(result, dict) and "error" not in result:
                group_id = (
                    result.get("groupId") or result.get("id") or result.get("groupID")
                    or (result.get("result") or {}).get("groupId")
                )
                if group_id:
                    break

        if group_id:
            group_id = str(group_id)
            self.group_allow_from.add(group_id)
            try:
                self._store.add_dynamic_group(group_id)
            except Exception as e:
                logger.warning("Signal: failed to persist dynamic group: %s", e)
            audit("group_created", group=group_id, name=name)
            logger.info("Signal: created group '%s' -> %s", name, group_id)
            return group_id

        logger.warning("Signal: failed to create group '%s'", name)
        return None
