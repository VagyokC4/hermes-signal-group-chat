"""Stateless helpers shared across the signal-group-chat plugin.

These are lifted verbatim from the previous forked signal.py so behavior is
identical; they live here (rather than in the adapter) so the adapter stays a
thin set of overrides over the upstream SignalAdapter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Upstream helper — reused so comma parsing matches the core adapter exactly.
from gateway.platforms.signal import _parse_comma_list  # noqa: F401  (re-exported)

# --- delete-watch tuning ---------------------------------------------------
DELETE_CACHE_TTL_SECONDS = 26 * 60 * 60          # a little longer than Signal's delete window
DELETE_CACHE_PRUNE_INTERVAL_SECONDS = 15 * 60    # opportunistic cleanup cadence
DELETE_WATCH_DEFAULT_NAMES = ("Calm Serenity",)
DELETE_ALERT_DEFAULT_NAME = "Hermes Agent"


def coerce_str_list(value: Any) -> list[str]:
    """Normalize a config/env value into a list of strings."""
    if not value:
        return []
    if isinstance(value, str):
        return _parse_comma_list(value)
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                out.extend(_parse_comma_list(item) if "," in item else [item.strip()])
            else:
                out.append(str(item).strip())
        return [item for item in out if item]
    return [str(value).strip()]


def normalize_token(value: Any) -> str:
    """Lowercase/strip a message token for alias matching."""
    if value is None:
        return ""
    return str(value).strip().lower()


def coerce_timestamp_ms(value: Any) -> int | None:
    """Best-effort conversion of a Signal timestamp to an integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def load_channel_directory() -> dict:
    """Load the local channel directory if present (/opt/data/channel_directory.json)."""
    path = Path("/opt/data/channel_directory.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def signal_directory_entries(name: str, chat_type: str | None = None) -> list[dict]:
    """Return Signal channel directory entries matching *name*."""
    directory = load_channel_directory()
    out: list[dict] = []
    wanted = normalize_token(name)
    for entry in directory.get("platforms", {}).get("signal", []):
        if not isinstance(entry, dict):
            continue
        if chat_type and entry.get("type") != chat_type:
            continue
        if normalize_token(entry.get("name")) == wanted or normalize_token(entry.get("id")) == wanted:
            out.append(entry)
    return out


def signal_entry_chat_id(entry: dict) -> str | None:
    """Map a channel directory entry to a gateway chat_id."""
    if not isinstance(entry, dict):
        return None
    entry_id = entry.get("id")
    if not entry_id:
        return None
    if entry.get("type") == "group" and not str(entry_id).startswith("group:"):
        return f"group:{entry_id}"
    return str(entry_id)


def extract_delete_timestamp_from_obj(obj: Any, _in_delete: bool = False) -> int | None:
    """Recursively search a Signal envelope for a delete timestamp."""
    if isinstance(obj, dict):
        for key in ("remoteDelete", "remoteDeleteMessage", "deleteMessage", "deletedMessage", "delete"):
            if key in obj:
                ts = extract_delete_timestamp_from_obj(obj.get(key), True)
                if ts is not None:
                    return ts
        for value in obj.values():
            ts = extract_delete_timestamp_from_obj(value, _in_delete)
            if ts is not None:
                return ts
    elif isinstance(obj, list):
        for item in obj:
            ts = extract_delete_timestamp_from_obj(item, _in_delete)
            if ts is not None:
                return ts
    elif _in_delete:
        return coerce_timestamp_ms(obj)
    return None


def message_tokens(*objects: Any) -> set:
    """Collect normalized identifiers from a Signal envelope/message."""
    tokens: set = set()
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for key in (
            "source", "sourceNumber", "sourceUuid", "sourceName",
            "destination", "destinationNumber", "destinationName",
            "recipient", "recipientName", "groupId", "groupName",
            "chatId", "chatName", "sender", "senderName",
        ):
            val = obj.get(key)
            if isinstance(val, list):
                for item in val:
                    token = normalize_token(item)
                    if token:
                        tokens.add(token)
            else:
                token = normalize_token(val)
                if token:
                    tokens.add(token)
        group_info = obj.get("groupInfo")
        if isinstance(group_info, dict):
            for key in ("groupId", "groupName"):
                token = normalize_token(group_info.get(key))
                if token:
                    tokens.add(token)
    return tokens
