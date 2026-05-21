"""SignalGroupChatAdapter — a thin subclass of the upstream Signal adapter.

It inherits all upstream behavior (RPC, SSE, markdown→Signal formatting, typing,
reactions, attachment fetch, rate limiting) and overrides only what we customize:
access control, delete-watch, file staging, and the /single ↔ /group mode system.

Registered under platform name "signal", so the gateway's plugin registry uses
this instead of the built-in adapter while all SIGNAL_* env/config keep working.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.helpers import redact_phone
from gateway.platforms.signal import (
    SIGNAL_MAX_ATTACHMENT_SIZE,
    _ext_to_mime,
    _render_mentions,
)
from gateway.platforms.signal import (
    SignalAdapter as _UpstreamSignalAdapter,
)

from . import commands, observability
from ._util import (
    DELETE_ALERT_DEFAULT_NAME,
    extract_delete_timestamp_from_obj,
    normalize_token,
)
from .access_control import AccessControlMixin
from .config_store import ConfigStore, plugin_setting
from .delete_watch import DeleteWatchMixin
from .group_buffer import GroupBuffer
from .modes import ModeManager
from .staging import stage_for_signal

logger = observability.logger


class SignalGroupChatAdapter(AccessControlMixin, DeleteWatchMixin, _UpstreamSignalAdapter):
    """Upstream Signal adapter + access control + delete-watch + group modes."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        extra = config.extra or {}

        # --- delete-watch state (which rooms are watched lives in the store,
        # OFF by default; alerts go to ONE central chat) ---
        self._delete_cache_path = Path(
            extra.get("delete_cache_path")
            or os.getenv("SIGNAL_DELETE_CACHE_PATH")
            or "/opt/data/signal-delete-cache.sqlite3"
        )
        self._delete_cache_conn = None
        self._delete_cache_lock = asyncio.Lock()
        self._delete_cache_last_prune = 0.0
        self._delete_cache_last_vacuum = 0.0
        self._delete_alert_chat_id = (
            extra.get("delete_alert_chat_id") or os.getenv("SIGNAL_DELETE_ALERT_CHAT_ID") or ""
        ).strip()
        self._delete_alert_chat_label = str(
            extra.get("delete_alert_chat_label")
            or os.getenv("SIGNAL_DELETE_ALERT_CHAT_LABEL")
            or DELETE_ALERT_DEFAULT_NAME
        ).strip()

        # --- backup-aware grant/mode store + group-mode helpers ---
        self._store = ConfigStore()
        self._modes = ModeManager(self._store)
        self._buffer = GroupBuffer(
            max_messages=int(plugin_setting("buffer_max_messages", 50) or 50),
            ttl_seconds=int(plugin_setting("buffer_ttl_seconds", 86400) or 86400),
        )

        logger.info(
            "signal-group-chat adapter initialized: url=%s account=%s default_mode=%s",
            self.http_url, redact_phone(self.account), self._modes.default_mode(),
        )

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> bool:
        ok = await super().connect()
        if ok:
            try:
                await self._resolve_alert_chat()
            except Exception as e:  # pragma: no cover
                logger.debug("delete-watch alert-chat resolve failed: %s", e)
        return ok

    # -- small overrides -----------------------------------------------------

    async def send_image(self, chat_id: str, image_url: str, caption: str | None = None, **kwargs):
        if image_url.startswith("file://"):
            staged = stage_for_signal(unquote(image_url[7:]))
            image_url = "file://" + quote(staged)
        return await super().send_image(chat_id, image_url, caption, **kwargs)

    async def _send_attachment(self, chat_id: str, file_path: str, media_label: str, caption: str | None = None):
        if file_path.startswith("file://"):
            file_path = unquote(file_path[7:])
        file_path = stage_for_signal(file_path)
        return await super()._send_attachment(chat_id, file_path, media_label, caption)

    # -- bot identity (for @mention / reply summon detection) ----------------

    def _account_aliases(self) -> set:
        ids = {self._account_normalized, self.account}
        own_uuid = self._recipient_uuid_by_number.get(self._account_normalized)
        if own_uuid:
            ids.add(own_uuid)
        return {normalize_token(x) for x in ids if x}

    # -- inbound -------------------------------------------------------------

    async def _handle_envelope(self, envelope: dict) -> None:
        """Process an incoming signal-cli envelope (overrides upstream)."""
        envelope_data = envelope.get("envelope", envelope)

        note_to_self_enabled = os.getenv("SIGNAL_NOTE_TO_SELF", "true").lower() not in {"false", "0", "no"}
        is_note_to_self = False
        is_sync_group_msg = False
        is_delete_watch_message = False
        sent_msg: dict | None = None
        if "syncMessage" in envelope_data:
            sync_msg = envelope_data.get("syncMessage")
            if sync_msg and isinstance(sync_msg, dict):
                sent_msg = sync_msg.get("sentMessage")
                if sent_msg and isinstance(sent_msg, dict):
                    dest = sent_msg.get("destinationNumber") or sent_msg.get("destination")
                    sent_ts = sent_msg.get("timestamp")
                    group_info = sent_msg.get("groupInfo")
                    sync_group_id = group_info.get("groupId") if group_info else None
                    if extract_delete_timestamp_from_obj(sent_msg) is not None:
                        # Owner deleted a message from their phone — promote so we process it.
                        is_delete_watch_message = True
                        envelope_data = {**envelope_data, "dataMessage": sent_msg}
                    if dest == self._account_normalized:
                        if sent_ts and sent_ts in self._recent_sent_timestamps:
                            self._recent_sent_timestamps.discard(sent_ts)
                            return
                        if not note_to_self_enabled:
                            return
                        is_note_to_self = True
                        envelope_data = {**envelope_data, "dataMessage": sent_msg}
                    elif sync_group_id and (
                        effective := self._effective_group_allowlist()
                    ) and ("*" in effective or sync_group_id in effective):
                        if sent_ts and sent_ts in self._recent_sent_timestamps:
                            self._recent_sent_timestamps.discard(sent_ts)
                            return
                        is_sync_group_msg = True
                        envelope_data = {**envelope_data, "dataMessage": sent_msg}
            if not is_note_to_self and not is_sync_group_msg and not is_delete_watch_message:
                return

        # Sender extraction (with sealed-sender sourceAddress fallback)
        _src_addr = envelope_data.get("sourceAddress") or {}
        sender = (
            envelope_data.get("sourceNumber")
            or envelope_data.get("sourceUuid")
            or envelope_data.get("source")
            or _src_addr.get("number")
            or _src_addr.get("uuid")
        )
        sender_name = envelope_data.get("sourceName", "")
        sender_uuid = envelope_data.get("sourceUuid", "")
        self._remember_recipient_identifiers(sender, sender_uuid)

        if not sender:
            logger.debug("Signal: ignoring envelope with no sender")
            return

        is_delete = extract_delete_timestamp_from_obj(envelope_data) is not None

        if (self._account_normalized and sender == self._account_normalized
                and not is_note_to_self and not is_sync_group_msg
                and not is_delete_watch_message and not is_delete):
            return

        if self.ignore_stories and envelope_data.get("storyMessage"):
            return

        data_message = (
            envelope_data.get("dataMessage")
            or (envelope_data.get("editMessage") or {}).get("dataMessage")
        )
        if not data_message:
            return

        ts_ms = envelope_data.get("timestamp", 0)
        if ts_ms:
            try:
                timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            except (ValueError, OSError):
                timestamp = datetime.now(tz=timezone.utc)
        else:
            timestamp = datetime.now(tz=timezone.utc)

        group_info = data_message.get("groupInfo")
        group_id = group_info.get("groupId") if group_info else None
        is_group = bool(group_id)

        if is_group:
            _eff = self._effective_group_allowlist()
            if not _eff:
                logger.debug("Signal: ignoring group message (no allowlist)")
                return
            if "*" not in _eff and group_id not in _eff:
                logger.debug("Signal: group %s not in allowlist", group_id[:8] if group_id else "?")
                return

        chat_id = sender if not is_group else f"group:{group_id}"
        chat_type = "group" if is_group else "dm"
        chat_name = (group_info.get("groupName") if group_info else sender_name) or chat_id

        # --- delete-watch: recover remote-delete events for watched chats ---
        delete_ts = extract_delete_timestamp_from_obj(data_message) or extract_delete_timestamp_from_obj(envelope_data)
        if delete_ts is not None:
            if self._delete_watch_enabled(chat_id):
                await self._handle_delete_watch_event(
                    chat_id=chat_id, room_label=chat_name, deleted_ts=delete_ts, data_message=data_message,
                )
            return

        text = data_message.get("message", "")
        mentions = data_message.get("mentions", [])
        if text and mentions:
            text = _render_mentions(text, mentions)

        quote_data = data_message.get("quote") or {}
        reply_to_id = str(quote_data.get("id")) if quote_data.get("id") else None
        reply_to_text = quote_data.get("text")

        attachments_data = data_message.get("attachments", [])
        media_urls = []
        media_types = []
        if attachments_data and not getattr(self, "ignore_attachments", False):
            for att in attachments_data:
                att_id = att.get("id")
                att_size = att.get("size", 0)
                if not att_id:
                    continue
                if att_size > SIGNAL_MAX_ATTACHMENT_SIZE:
                    logger.warning("Signal: attachment too large (%d bytes), skipping", att_size)
                    continue
                try:
                    cached_path, ext = await self._fetch_attachment(att_id)
                    if cached_path:
                        content_type = att.get("contentType") or _ext_to_mime(ext)
                        media_urls.append(cached_path)
                        media_types.append(content_type)
                except Exception:
                    logger.exception("Signal: failed to fetch attachment %s", att_id)

        if (not text or not text.strip()) and not media_urls:
            logger.debug("Signal: skipping contentless envelope from %s", redact_phone(sender))
            return

        source = self.build_source(
            chat_id=chat_id,
            chat_name=group_info.get("groupName") if group_info else sender_name,
            chat_type=chat_type,
            user_id=sender,
            user_name=sender_name or sender,
            user_id_alt=sender_uuid if sender_uuid else None,
            chat_id_alt=group_id if is_group else None,
        )

        # --- delete-watch: capture so a future delete can be recovered (non-terminal) ---
        if self._delete_watch_enabled(chat_id):
            await self._store_delete_watch_message(
                chat_id=chat_id, room_label=chat_name,
                sender_id=sender or "", sender_name=sender_name or "",
                timestamp_ms=ts_ms or int(time.time() * 1000),
                text=text or "", data_message=data_message,
            )

        msg_type = MessageType.TEXT
        if media_types:
            if any(mt.startswith("audio/") for mt in media_types):
                msg_type = MessageType.VOICE
            elif any(mt.startswith("image/") for mt in media_types):
                msg_type = MessageType.PHOTO

        event = MessageEvent(
            source=source,
            text=text or "",
            message_type=msg_type,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=timestamp,
            raw_message={"sender": sender, "timestamp_ms": ts_ms},
            reply_to_message_id=reply_to_id,
            reply_to_text=reply_to_text,
        )

        # --- DM access control: only the owner can DM ---
        if not is_group and not self._is_owner(sender):
            logger.debug("Signal: ignoring DM from non-owner %s", redact_phone(sender))
            return

        # --- group access control + /single|/group mode routing ---
        if is_group and group_id:
            dispatch_event = await self._route_group(
                event=event, group_id=group_id, group_info=group_info,
                sender=sender, sender_name=sender_name, text=text or "",
                data_message=data_message, ts_ms=ts_ms,
            )
            if dispatch_event is None:
                return
            event = dispatch_event

        await self.handle_message(event)

    # -- group routing (access control + modes) ------------------------------

    async def _route_group(self, *, event, group_id, group_info, sender, sender_name,
                           text, data_message, ts_ms) -> MessageEvent | None:
        """Return the event to dispatch, or None to stay silent.

        Owner admin commands are intercepted in any mode. In /group mode every
        message is buffered but only a *summon* triggers a reply (and only an
        approved/owner summoner is allowed through). In /single mode the classic
        approval gate applies to every message.
        """
        is_owner = self._is_owner(sender)

        # Owner admin commands work in any mode.
        if is_owner and text and commands.looks_like_command(text):
            response = await commands.handle_owner_command(self, text, group_id, sender)
            if response is not None:
                await self._send_group_message(group_id, response)
                return None

        mode = self._modes.get_mode(group_id)

        if mode == "group":
            observability.count(group_id, "seen")
            summoned, cleaned = self._modes.is_summon(
                group_id, text=text, mentions=data_message.get("mentions") or [],
                quote=data_message.get("quote") or {}, account_ids=self._account_aliases(),
            )
            if not summoned:
                self._buffer.append(group_id, sender_name or sender, text, ts_ms)
                return None
            if not is_owner and not self._is_approved(sender, group_id):
                await self._notify_unapproved(group_id, group_info, sender, sender_name)
                return None
            transcript = self._buffer.render(group_id)
            event.text = self._modes.compose_summon_prompt(transcript, cleaned, sender_name or sender)
            self._buffer.append(group_id, sender_name or sender, text, ts_ms)
            observability.count(group_id, "responded")
            observability.audit("summon", group=group_id, actor=sender)
            return event

        # single mode: classic approval gate
        if not is_owner and not self._is_approved(sender, group_id):
            await self._notify_unapproved(group_id, group_info, sender, sender_name)
            return None
        return event

    async def _notify_unapproved(self, group_id, group_info, sender, sender_name) -> None:
        """Tell the member they need approval and ping the owner (two-message UX)."""
        pending_msg = (
            f"\U0001f44b Hi {sender_name or 'there'}! I'm Hermes. "
            "You need the group owner's approval before I can respond to you. "
            "Ask them to type: /approve " + (sender or "your-phone-number")
        )
        await self._send_group_message(group_id, pending_msg)
        home = os.getenv("SIGNAL_HOME_CHANNEL", "")
        if home and home.startswith("group:"):
            group_display = (group_info.get("groupName") if group_info else None) or (group_id[:8] if group_id else "a group")
            owner_notice = (
                f"⚠️ Access request: {sender_name or sender} "
                f"({redact_phone(sender)}) wants to chat in \"{group_display}\". "
                f"Forward or paste the next message into that group:"
            )
            await self._rpc("send", {"account": self.account, "message": owner_notice, "groupId": home[6:]})
            await self._rpc("send", {"account": self.account, "message": f"/approve {sender}", "groupId": home[6:]})
        observability.audit("access_request", group=group_id, actor=sender)
        logger.info("Signal: blocked unapproved group member %s, notified owner", redact_phone(sender))
