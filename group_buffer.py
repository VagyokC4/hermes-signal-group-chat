"""Per-group rolling transcript buffer for /group mode.

In /group mode Hermes stays silent but records group messages so that, when it
IS summoned, it can answer with the recent conversation as context. The buffer
is intentionally separate from Hermes's own session store so it does not depend
on ``group_sessions_per_user`` and we keep full control of the retention window.

Persisted under HERMES_HOME (full-backup covered) so context survives restarts.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from .observability import logger

_BUFFER_DIR = Path("/opt/data/plugins/signal-group-chat/buffers")


class GroupBuffer:
    """Bounded, TTL'd, attributed message buffer keyed by group id."""

    def __init__(self, max_messages: int = 50, ttl_seconds: int = 24 * 60 * 60,
                 max_render_chars: int = 6000):
        self.max_messages = max_messages
        self.ttl_seconds = ttl_seconds
        self.max_render_chars = max_render_chars
        self._lock = threading.RLock()
        self._mem: dict[str, deque[dict]] = {}

    def _file(self, group_id: str) -> Path:
        digest = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:16]
        return _BUFFER_DIR / f"{digest}.json"

    def _get(self, group_id: str) -> deque[dict]:
        if group_id in self._mem:
            return self._mem[group_id]
        dq: deque[dict] = deque(maxlen=self.max_messages)
        try:
            f = self._file(group_id)
            if f.exists():
                for row in json.loads(f.read_text(encoding="utf-8")):
                    dq.append(row)
        except Exception as exc:
            logger.debug("group buffer load failed for %s: %s", group_id[:8], exc)
        self._mem[group_id] = dq
        return dq

    def _persist(self, group_id: str, dq: deque[dict]) -> None:
        try:
            _BUFFER_DIR.mkdir(parents=True, exist_ok=True)
            f = self._file(group_id)
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(list(dq), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, f)
        except Exception as exc:
            logger.debug("group buffer persist failed for %s: %s", group_id[:8], exc)

    def _prune(self, dq: deque[dict]) -> None:
        cutoff = time.time() - self.ttl_seconds
        while dq and dq[0].get("ts", 0) / 1000.0 < cutoff:
            dq.popleft()

    def append(self, group_id: str, author: str, text: str, ts_ms: int) -> None:
        if not (text and text.strip()):
            return
        with self._lock:
            dq = self._get(group_id)
            dq.append({"author": author or "unknown", "text": text.strip(),
                       "ts": int(ts_ms or time.time() * 1000)})
            self._prune(dq)
            self._persist(group_id, dq)

    def render(self, group_id: str) -> str:
        """Return an attributed transcript of recent messages (oldest first)."""
        with self._lock:
            dq = self._get(group_id)
            self._prune(dq)
            lines: list[str] = []
            for row in dq:
                stamp = time.strftime("%H:%M", time.localtime(row.get("ts", 0) / 1000.0))
                lines.append(f"[{row.get('author', '?')} {stamp}] {row.get('text', '')}")
        text = "\n".join(lines)
        if len(text) > self.max_render_chars:
            text = "…" + text[-self.max_render_chars:]
        return text

    def size(self, group_id: str) -> int:
        with self._lock:
            dq = self._get(group_id)
            self._prune(dq)
            return len(dq)

    def clear(self, group_id: str) -> None:
        with self._lock:
            self._mem.pop(group_id, None)
            try:
                self._file(group_id).unlink(missing_ok=True)
            except Exception:
                pass
