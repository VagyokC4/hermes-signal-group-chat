"""Deleted-message watching with a small local SQLite cache.

A mixin over the upstream SignalAdapter. Watches one configured contact/room;
caches its messages and, when a remote-delete event arrives, recovers the
original text and forwards it to a configured alert chat.

Lifted from the previous forked signal.py (behavior-preserving).
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from gateway.platforms.signal import MAX_MESSAGE_LENGTH

from ._util import (
    DELETE_CACHE_PRUNE_INTERVAL_SECONDS,
    DELETE_CACHE_TTL_SECONDS,
    message_tokens,
    normalize_token,
    signal_directory_entries,
    signal_entry_chat_id,
)
from .observability import logger


class DeleteWatchMixin:
    # adapter provides: self._delete_* attributes, self._rpc, self.account,
    # self._resolve_recipient, self._track_sent_timestamp

    def _get_delete_cache_conn(self) -> sqlite3.Connection:
        if self._delete_cache_conn is None:
            self._delete_cache_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._delete_cache_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deleted_message_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_chat_id TEXT NOT NULL,
                    room_label TEXT NOT NULL,
                    sender_id TEXT,
                    sender_name TEXT,
                    message_ts INTEGER NOT NULL,
                    message_text TEXT,
                    message_json TEXT NOT NULL,
                    captured_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_deleted_message_cache_lookup
                    ON deleted_message_cache(room_chat_id, message_ts);
                CREATE INDEX IF NOT EXISTS idx_deleted_message_cache_expiry
                    ON deleted_message_cache(expires_at);

                CREATE TABLE IF NOT EXISTS processed_delete_events (
                    room_chat_id TEXT NOT NULL,
                    deleted_ts INTEGER NOT NULL,
                    observed_at INTEGER NOT NULL,
                    PRIMARY KEY(room_chat_id, deleted_ts)
                );
                """
            )
            self._delete_cache_conn = conn
        return self._delete_cache_conn

    def _delete_watch_storage_room_id(self) -> str:
        if self._delete_watch_canonical_ids:
            return sorted(self._delete_watch_canonical_ids)[0]
        if self._delete_watch_aliases:
            return sorted(self._delete_watch_aliases)[0]
        return self._delete_watch_name or "delete-watch"

    async def _prime_delete_watch_aliases(self) -> None:
        if not self._delete_watch_active:
            return

        aliases = set(self._delete_watch_aliases)
        labels = set(self._delete_watch_labels)
        canonical_ids = set(self._delete_watch_canonical_ids)

        for candidate in {self._delete_watch_name, self._delete_alert_chat_label}:
            if not candidate:
                continue
            for entry in signal_directory_entries(candidate):
                entry_id = signal_entry_chat_id(entry)
                entry_name = str(entry.get("name") or "").strip()
                if entry_id:
                    canonical_ids.add(entry_id)
                    aliases.add(normalize_token(entry_id))
                if entry_name:
                    aliases.add(normalize_token(entry_name))
                    labels.add(entry_name)

        if not self._delete_alert_chat_id and self._delete_alert_chat_label:
            for entry in signal_directory_entries(self._delete_alert_chat_label, chat_type="group"):
                entry_id = signal_entry_chat_id(entry)
                if entry_id:
                    self._delete_alert_chat_id = entry_id
                    break

        try:
            contacts = await self._rpc("listContacts", {"account": self.account}) or []
        except Exception as e:
            logger.debug("Signal: delete-watch contact priming failed: %s", e)
            contacts = []

        for contact in contacts:
            if not isinstance(contact, dict):
                continue
            name = str(contact.get("name") or contact.get("profileName") or "").strip()
            recipient = str(contact.get("number") or contact.get("recipient") or "").strip()
            service_id = str(contact.get("uuid") or contact.get("serviceId") or "").strip()
            if not name and not recipient and not service_id:
                continue

            match_name = normalize_token(name) == normalize_token(self._delete_watch_name)
            match_id = normalize_token(service_id) in aliases or normalize_token(recipient) in aliases
            if not (match_name or match_id):
                continue

            if name:
                aliases.add(normalize_token(name))
                labels.add(name)
            if recipient:
                aliases.add(normalize_token(recipient))
            if service_id:
                aliases.add(normalize_token(service_id))
                canonical_ids.add(service_id)
                self._delete_watch_canonical_ids.add(service_id)

        self._delete_watch_aliases = aliases
        self._delete_watch_labels = labels or self._delete_watch_labels
        self._delete_watch_canonical_ids = canonical_ids
        self._delete_watch_active = bool(self._delete_watch_aliases)

    def _message_matches_delete_watch(self, *objects: Any) -> bool:
        if not self._delete_watch_active:
            return False
        return bool(message_tokens(*objects) & self._delete_watch_aliases)

    async def _prune_delete_cache(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._delete_cache_last_prune < DELETE_CACHE_PRUNE_INTERVAL_SECONDS:
            return
        async with self._delete_cache_lock:
            conn = self._get_delete_cache_conn()
            now_ms = int(now * 1000)
            conn.execute("DELETE FROM deleted_message_cache WHERE expires_at <= ?", (now_ms,))
            conn.execute(
                "DELETE FROM processed_delete_events WHERE observed_at <= ?",
                (now_ms - DELETE_CACHE_TTL_SECONDS * 1000,),
            )
            conn.commit()
            self._delete_cache_last_prune = now

    async def _store_delete_watch_message(
        self,
        *,
        room_chat_id: str,
        room_label: str,
        sender_id: str,
        sender_name: str,
        timestamp_ms: int,
        data_message: dict,
        envelope_data: dict,
    ) -> None:
        if not self._delete_watch_active:
            return

        payload = {
            "room_chat_id": room_chat_id,
            "room_label": room_label,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "timestamp_ms": timestamp_ms,
            "data_message": data_message,
            "envelope": envelope_data,
        }
        body = str(data_message.get("message") or "")
        now_ms = int(time.time() * 1000)
        expires_at = now_ms + DELETE_CACHE_TTL_SECONDS * 1000

        async with self._delete_cache_lock:
            conn = self._get_delete_cache_conn()
            conn.execute(
                """
                INSERT INTO deleted_message_cache (
                    room_chat_id, room_label, sender_id, sender_name,
                    message_ts, message_text, message_json, captured_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_chat_id, room_label, sender_id, sender_name,
                    timestamp_ms, body, json.dumps(payload, ensure_ascii=False),
                    now_ms, expires_at,
                ),
            )
            conn.commit()
        await self._prune_delete_cache()

    async def _lookup_delete_watch_message(self, room_chat_id: str, deleted_ts: int) -> dict | None:
        async with self._delete_cache_lock:
            conn = self._get_delete_cache_conn()
            rows = conn.execute(
                """
                SELECT * FROM deleted_message_cache
                WHERE room_chat_id = ? AND message_ts BETWEEN ? AND ?
                ORDER BY ABS(message_ts - ?), captured_at DESC LIMIT 3
                """,
                (room_chat_id, deleted_ts - 2000, deleted_ts + 2000, deleted_ts),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    """
                    SELECT * FROM deleted_message_cache
                    WHERE room_chat_id = ? AND message_ts = ?
                    ORDER BY captured_at DESC LIMIT 3
                    """,
                    (room_chat_id, deleted_ts),
                ).fetchall()
        if not rows:
            return None
        row = rows[0]
        try:
            payload = json.loads(row["message_json"]) if row["message_json"] else {}
        except Exception:
            payload = {}
        return {
            "room_chat_id": row["room_chat_id"],
            "room_label": row["room_label"],
            "sender_id": row["sender_id"],
            "sender_name": row["sender_name"],
            "timestamp_ms": row["message_ts"],
            "message_text": row["message_text"],
            "captured_at": row["captured_at"],
            "expires_at": row["expires_at"],
            "payload": payload,
        }

    async def _notify_delete_watch_message(self, record: dict, deleted_ts: int) -> None:
        alert_chat_id = self._delete_alert_chat_id
        if not alert_chat_id:
            logger.warning("Signal delete-watch: no alert chat configured; logging only")
            logger.info("Signal delete-watch: %s", record)
            return

        room_label = record.get("room_label") or self._delete_watch_name
        sender_name = record.get("sender_name") or record.get("sender_id") or "unknown"
        message_text = str(record.get("message_text") or "").strip()
        payload = record.get("payload") or {}
        data_message = payload.get("data_message") if isinstance(payload, dict) else {}
        attachments = data_message.get("attachments") or [] if isinstance(data_message, dict) else []

        lines = [
            f"Deleted message recovered from {room_label}",
            f"Sender: {sender_name}",
            f"Original timestamp: {record.get('timestamp_ms')}",
            f"Delete timestamp: {deleted_ts}",
        ]
        if message_text:
            lines += ["", message_text]
        elif attachments:
            lines += ["", f"[attachment-only message with {len(attachments)} attachment(s)]"]
        else:
            lines += ["", "[message body unavailable]"]

        alert_text = "\n".join(lines)
        if len(alert_text) > MAX_MESSAGE_LENGTH:
            alert_text = alert_text[: MAX_MESSAGE_LENGTH - 20] + "\n…[truncated]"

        params: dict[str, Any] = {"account": self.account, "message": alert_text}
        if alert_chat_id.startswith("group:"):
            params["groupId"] = alert_chat_id[6:]
        else:
            params["recipient"] = [await self._resolve_recipient(alert_chat_id)]

        result = await self._rpc("send", params)
        if result is not None:
            self._track_sent_timestamp(result)
            logger.info("Signal delete-watch: sent recovery notice room=%s ts=%s", room_label, deleted_ts)
        else:
            logger.warning("Signal delete-watch: failed to send recovery notice room=%s ts=%s", room_label, deleted_ts)

    async def _handle_delete_watch_event(
        self,
        *,
        room_chat_id: str,
        deleted_ts: int,
        envelope_data: dict,
        data_message: dict | None = None,
    ) -> None:
        await self._prune_delete_cache()
        async with self._delete_cache_lock:
            conn = self._get_delete_cache_conn()
            inserted = conn.execute(
                "INSERT OR IGNORE INTO processed_delete_events(room_chat_id, deleted_ts, observed_at) VALUES (?, ?, ?)",
                (room_chat_id, deleted_ts, int(time.time() * 1000)),
            ).rowcount
            conn.commit()
        if not inserted:
            logger.debug("Signal delete-watch: duplicate delete event ignored room=%s ts=%s", room_chat_id, deleted_ts)
            return
        record = await self._lookup_delete_watch_message(room_chat_id, deleted_ts)
        if record is None:
            logger.warning("Signal delete-watch: delete event for %s ts=%s had no cached message", room_chat_id, deleted_ts)
            return
        await self._notify_delete_watch_message(record, deleted_ts)
