"""Structured logging, an append-only audit log, and lightweight counters.

The audit log records security- and mode-relevant events (approvals, revokes,
mode changes, summons) so operators can answer "who changed what, when" after
the fact. It lives under HERMES_HOME so it is captured by full backups.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes.plugins.signal_group_chat")

_AUDIT_PATH = Path("/opt/data/signal-audit.jsonl")
_audit_lock = threading.Lock()

# In-memory per-group counters: {group_id: {"seen": int, "responded": int, ...}}
_counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
_counters_lock = threading.Lock()


def audit(event: str, *, group: str = "", actor: str = "", **detail: Any) -> None:
    """Append one structured audit record. Best-effort; never raises."""
    record = {
        "ts": int(time.time() * 1000),
        "event": event,
        "group": _short(group),
        "actor": _redact(actor),
        **detail,
    }
    try:
        with _audit_lock:
            _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_AUDIT_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover
        logger.debug("audit write failed: %s", exc)


def count(group_id: str, metric: str, n: int = 1) -> None:
    with _counters_lock:
        _counters[group_id][metric] += n


def snapshot(group_id: str) -> dict[str, int]:
    with _counters_lock:
        return dict(_counters.get(group_id, {}))


def _short(value: str) -> str:
    if value and value.startswith("group:") and len(value) > 14:
        return value[:14] + "…"
    return value or ""


def _redact(value: str) -> str:
    """Mask the middle of a phone number; leave UUIDs/short tokens intact."""
    if value and value.startswith("+") and len(value) >= 7:
        return value[:3] + "***" + value[-2:]
    return value or ""
