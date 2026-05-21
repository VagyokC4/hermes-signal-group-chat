"""send_message tool override.

The upstream ``send_message`` tool talks to signal-cli directly (not through the
adapter), so adapter-level file staging is bypassed. We register an override
(same name, ``override=True``) that wraps the upstream handler and adds, at the
call boundary:

  1. an ``attachments=[...]`` convenience param (translated to MEDIA: tags),
  2. automatic staging of MEDIA: file paths into the shared /opt/data volume so
     signal-cli (separate container) can read them,
  3. session-target fallback — when ``target`` is omitted and we're inside a
     platform session, deliver to the active conversation, not the home channel.

This keeps us off a 1900-line fork: we delegate to the upstream handler.
"""

from __future__ import annotations

import copy
import re

from .observability import logger
from .staging import stage_for_signal

_MEDIA_RE = re.compile(r"MEDIA:(\S+)")


def _augmented_schema(base: dict) -> dict:
    schema = copy.deepcopy(base)
    props = schema.setdefault("parameters", {}).setdefault("properties", {})
    props["attachments"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Optional list of absolute local file paths to attach (Signal). Files are "
            "automatically copied into the shared /opt/data volume so signal-cli can "
            "deliver them. Cleaner than embedding MEDIA: tags."
        ),
    }
    return schema


def _stage_media_in_message(message: str) -> str:
    return _MEDIA_RE.sub(lambda m: "MEDIA:" + stage_for_signal(m.group(1)), message)


def _session_target() -> str:
    try:
        from gateway.session_context import get_session_env

        platform = (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower()
        chat_id = (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
        if platform and platform != "local":
            return f"{platform}:{chat_id}" if chat_id else platform
    except Exception:
        pass
    return ""


def register_tools(ctx) -> None:
    """Register the send_message override. Best-effort; never fatal to load."""
    from tools.send_message_tool import (
        SEND_MESSAGE_SCHEMA,
        _check_send_message,
    )
    from tools.send_message_tool import (
        send_message_tool as _upstream,
    )

    def signal_aware_send_message(args, **kw):
        try:
            data = dict(args or {})
            if data.get("action", "send") == "send":
                message = data.get("message", "") or ""
                attachments = data.pop("attachments", None) or []
                if attachments:
                    prefix = " ".join(f"MEDIA:{str(p).strip()}" for p in attachments if p and str(p).strip())
                    message = f"{prefix} {message}".strip()
                if not data.get("target"):
                    tgt = _session_target()
                    if tgt:
                        data["target"] = tgt
                if message:
                    message = _stage_media_in_message(message)
                data["message"] = message
            return _upstream(data, **kw)
        except Exception as exc:  # never break the tool — fall back to upstream
            logger.warning("signal-group-chat send_message wrapper error: %s", exc)
            return _upstream(args, **kw)

    ctx.register_tool(
        name="send_message",
        toolset="messaging",
        schema=_augmented_schema(SEND_MESSAGE_SCHEMA),
        handler=signal_aware_send_message,
        check_fn=_check_send_message,
        emoji="📨",
        override=True,
    )
    logger.info("signal-group-chat: registered send_message override (staging + attachments)")
