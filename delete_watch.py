"""Deleted-message watching across ALL chats (opt-out per room).

Model: Hermes captures every message it sees (in chats where delete-watch is
enabled — on by default) into a small SQLite cache keyed by the message's real
chat id. When a "delete for everyone" event arrives for an enabled chat, the
original is recovered and forwarded to ONE central alert chat (the home group),
never back into the room where the delete happened.

Retention follows Signal's delete window: a message can only be deleted-for-
everyone within 24h of sending, so we keep cache rows for 24h + a 2h safety
buffer (26h total). Past that a message can never be deleted, so it's pruned.
A global row cap + periodic VACUUM are safety backstops against disk growth.

A mixin over the upstream SignalAdapter.
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
    signal_directory_entries,
    signal_entry_chat_id,
)
from .config_store import plugin_setting
from .observability import audit, logger

# Safety backstops (the TTL is the real limiter; these guard against runaway growth).
_VACUUM_INTERVAL_SECONDS = 6 * 60 * 60


class DeleteWatchMixin:
    # adapter provides: self._delete_cache_path/_conn/_lock/_last_prune/_last_vacuum,
    # self._delete_alert_chat_id, self._delete_alert_chat_label, self._store,
    # self._rpc, self.account, self._resolve_recipient, self._track_sent_timestamp

    # -- enablement ----------------------------------------------------------

    def _delete_watch_enabled(self, chat_id: str) -> bool:
        try:
            return self._store.delete_watch_enabled(chat_id)
        except Exception:
            return True

    def _delete_watch_max_rows(self) -> int:
        try:
            return int(plugin_setting("delete_watch_max_rows", 50000, env="SIGNAL_DELETE_MAX_ROWS") or 50000)
        except (TypeError, ValueError):
            return 50000

    # -- alert chat resolution (central; no per-room leakage) ----------------

    async def _resolve_alert_chat(self) -> None:
        """Resolve the central alert chat id once (env > label lookup > home channel)."""
        if not self._delete_alert_chat_id and self._delete_alert_chat_label:
            for entry in signal_directory_entries(self._delete_alert_chat_label, chat_type="group"):
                entry_id = signal_entry_chat_id(entry)
                if entry_id:
                    self._delete_alert_chat_id = entry_id
                    break
        if not self._delete_alert_chat_id:
            import os

            home = os.getenv("SIGNAL_HOME_CHANNEL", "").strip()
            if home:
                self._delete_alert_chat_id = home

    # -- SQLite cache --------------------------------------------------------

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

    @staticmethod
    def _compact_payload(data_message: dict) -> str:
        """Store only what's needed to describe a recovered message (bounds row size)."""
        atts = []
        for a in (data_message.get("attachments") or []) if isinstance(data_message, dict) else []:
            if isinstance(a, dict):
                atts.append({"contentType": a.get("contentType"), "filename": a.get("filename")})
        return json.dumps({"attachments": atts}, ensure_ascii=False)

    async def _store_delete_watch_message(
        self,
        *,
        chat_id: str,
        room_label: str,
        sender_id: str,
        sender_name: str,
        timestamp_ms: int,
        text: str,
        data_message: dict,
    ) -> None:
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
                    chat_id, room_label or "", sender_id or "", sender_name or "",
                    timestamp_ms, text or "", self._compact_payload(data_message),
                    now_ms, expires_at,
                ),
            )
            conn.commit()
        await self._prune_delete_cache()

    async def _lookup_delete_watch_message(self, chat_id: str, deleted_ts: int) -> dict | None:
        async with self._delete_cache_lock:
            conn = self._get_delete_cache_conn()
            rows = conn.execute(
                """
                SELECT * FROM deleted_message_cache
                WHERE room_chat_id = ? AND message_ts BETWEEN ? AND ?
                ORDER BY ABS(message_ts - ?), captured_at DESC LIMIT 1
                """,
                (chat_id, deleted_ts - 2000, deleted_ts + 2000, deleted_ts),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT * FROM deleted_message_cache WHERE room_chat_id = ? AND message_ts = ? LIMIT 1",
                    (chat_id, deleted_ts),
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
            "payload": payload,
        }

    async def _notify_delete_watch_message(self, record: dict, deleted_ts: int) -> None:
        if not self._delete_alert_chat_id:
            await self._resolve_alert_chat()
        alert_chat_id = self._delete_alert_chat_id
        if not alert_chat_id:
            logger.warning("Signal delete-watch: no alert chat configured; logging only")
            logger.info("Signal delete-watch recovered: %s", record)
            return

        room_label = record.get("room_label") or record.get("room_chat_id") or "a chat"
        sender_name = record.get("sender_name") or record.get("sender_id") or "unknown"
        message_text = str(record.get("message_text") or "").strip()
        atts = (record.get("payload") or {}).get("attachments") or []

        lines = [
            f"🗑️ Deleted message recovered from {room_label}",
            f"Sender: {sender_name}",
        ]
        if message_text:
            lines += ["", message_text]
        elif atts:
            lines += ["", f"[attachment-only message with {len(atts)} attachment(s)]"]
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
            audit("delete_recovered", group=record.get("room_chat_id", ""), actor=record.get("sender_id", ""))
            logger.info("Signal delete-watch: recovery notice sent (room=%s ts=%s)", room_label, deleted_ts)
        else:
            logger.warning("Signal delete-watch: failed to send recovery notice (room=%s)", room_label)

    async def _handle_delete_watch_event(
        self, *, chat_id: str, room_label: str, deleted_ts: int, data_message: dict | None = None
    ) -> None:
        await self._prune_delete_cache()
        async with self._delete_cache_lock:
            conn = self._get_delete_cache_conn()
            inserted = conn.execute(
                "INSERT OR IGNORE INTO processed_delete_events(room_chat_id, deleted_ts, observed_at) VALUES (?, ?, ?)",
                (chat_id, deleted_ts, int(time.time() * 1000)),
            ).rowcount
            conn.commit()
        if not inserted:
            logger.debug("Signal delete-watch: duplicate delete event ignored (room=%s ts=%s)", chat_id, deleted_ts)
            return
        record = await self._lookup_delete_watch_message(chat_id, deleted_ts)
        if record is None:
            logger.info(
                "Signal delete-watch: delete in %s (ts=%s) had no cached original (sent before capture or expired)",
                chat_id, deleted_ts,
            )
            return
        await self._notify_delete_watch_message(record, deleted_ts)

    # -- cleanup -------------------------------------------------------------

    async def _prune_delete_cache(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._delete_cache_last_prune < DELETE_CACHE_PRUNE_INTERVAL_SECONDS:
            return
        async with self._delete_cache_lock:
            conn = self._get_delete_cache_conn()
            now_ms = int(now * 1000)
            # 1. TTL: drop anything past Signal's delete window (+buffer).
            deleted = conn.execute("DELETE FROM deleted_message_cache WHERE expires_at <= ?", (now_ms,)).rowcount
            conn.execute(
                "DELETE FROM processed_delete_events WHERE observed_at <= ?",
                (now_ms - DELETE_CACHE_TTL_SECONDS * 1000,),
            )
            # 2. Size backstop: cap total rows, evicting oldest.
            max_rows = self._delete_watch_max_rows()
            total = conn.execute("SELECT COUNT(*) FROM deleted_message_cache").fetchone()[0]
            if total > max_rows:
                over = total - max_rows
                conn.execute(
                    "DELETE FROM deleted_message_cache WHERE id IN "
                    "(SELECT id FROM deleted_message_cache ORDER BY expires_at ASC LIMIT ?)",
                    (over,),
                )
                deleted += over
                logger.info("Signal delete-watch: evicted %d oldest rows (cap=%d)", over, max_rows)
            conn.commit()
            self._delete_cache_last_prune = now
            # 3. Reclaim disk periodically (SQLite doesn't shrink the file on DELETE).
            last_vac = getattr(self, "_delete_cache_last_vacuum", 0.0)
            if deleted and (now - last_vac > _VACUUM_INTERVAL_SECONDS):
                try:
                    conn.execute("VACUUM")
                    self._delete_cache_last_vacuum = now
                    logger.debug("Signal delete-watch: VACUUM reclaimed cache file space")
                except Exception as exc:  # pragma: no cover
                    logger.debug("Signal delete-watch: VACUUM failed: %s", exc)

    def delete_cache_stats(self) -> dict:
        """Stats for /delete-watch status: depth, staleness, ages, per-room, recoveries."""
        out = {
            "rows": 0, "rooms": 0, "stale": 0, "fresh": 0,
            "oldest_age_min": 0, "newest_age_min": 0, "top_rooms": [],
            "processed_events": 0, "max_rows": self._delete_watch_max_rows(),
            "ttl_hours": round(DELETE_CACHE_TTL_SECONDS / 3600, 1),
            "db_bytes": 0,
        }
        try:
            now_ms = int(time.time() * 1000)
            conn = self._get_delete_cache_conn()
            r = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT room_chat_id), MIN(message_ts), MAX(message_ts), "
                "SUM(CASE WHEN expires_at <= ? THEN 1 ELSE 0 END) FROM deleted_message_cache",
                (now_ms,),
            ).fetchone()
            total = r[0] or 0
            stale = r[4] or 0
            out.update(
                rows=total,
                rooms=r[1] or 0,
                stale=stale,
                fresh=total - stale,
                oldest_age_min=int((now_ms - r[2]) / 60000) if r[2] else 0,
                newest_age_min=int((now_ms - r[3]) / 60000) if r[3] else 0,
            )
            out["top_rooms"] = [
                (row["room_label"] or row["room_chat_id"], row["c"])
                for row in conn.execute(
                    "SELECT room_label, room_chat_id, COUNT(*) c FROM deleted_message_cache "
                    "GROUP BY room_chat_id ORDER BY c DESC LIMIT 5"
                ).fetchall()
            ]
            out["processed_events"] = conn.execute("SELECT COUNT(*) FROM processed_delete_events").fetchone()[0]
            try:
                out["db_bytes"] = self._delete_cache_path.stat().st_size
            except OSError:
                pass
        except Exception as exc:  # pragma: no cover
            logger.debug("delete_cache_stats failed: %s", exc)
        return out
