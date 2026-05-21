"""Layered, backup-aware configuration & runtime-grant store.

Resolution precedence (high -> low):
  1. Runtime state store  — this file's JSON, writable at runtime for real-time
     auth/revoke/mode changes. Stored under ``/opt/data/platforms/pairing/`` so
     it rides Hermes's pre-update *quick snapshots* (the ``platforms/pairing``
     allowlist entry) **and** full backups.
  2. config.yaml          — ``plugins.entries.signal-group-chat.*`` declarative
     policy (default mode, base settings). In quick + full backups.
  3. Environment vars     — ``SIGNAL_*`` defaults / bootstrap (back-compat).

On first run the store is seeded from the legacy JSON files and env so nothing
is lost in the migration; thereafter the store is authoritative for grants.

Reads are mtime+size cached (no JSON re-parse per inbound message). Writes are
atomic (temp file + os.replace) under a process lock and chmod 0600 (the file
holds phone numbers / service-ids = PII).
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from .observability import logger

STORE_PATH = Path("/opt/data/platforms/pairing/signal-group-chat.json")
_LEGACY_APPROVED = Path("/opt/data/signal_approved_members.json")
_LEGACY_DYNAMIC_GROUPS = Path("/opt/data/signal_dynamic_groups.json")
_LEGACY_MODES = Path("/opt/data/signal_group_modes.json")

_VALID_MODES = {"single", "group"}

# E.164 phone or a Signal service-id (UUID, optionally PNI:-prefixed).
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_UUID_RE = re.compile(r"^(PNI:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def is_valid_identifier(value: str) -> bool:
    """True if *value* looks like a Signal phone number or service-id."""
    v = (value or "").strip()
    return bool(_E164_RE.match(v) or _UUID_RE.match(v))


def plugin_setting(key: str, default: Any = None, *, env: str | None = None) -> Any:
    """Read a declarative setting: config.yaml plugins.entries first, then env."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        entries = (((cfg.get("plugins") or {}).get("entries") or {}).get("signal-group-chat") or {})
        if isinstance(entries, dict) and key in entries and entries[key] not in (None, ""):
            return entries[key]
    except Exception:
        pass
    if env:
        val = os.getenv(env)
        if val not in (None, ""):
            return val
    return default


class ConfigStore:
    """Thread-safe accessor for the runtime grant/mode store."""

    def __init__(self, path: Path = STORE_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._cache: dict | None = None
        self._cache_sig: tuple | None = None
        self._ensure_seeded()

    # -- low-level load/save -------------------------------------------------

    def _default_doc(self) -> dict:
        return {"version": 1, "approved": {}, "global_approved": [], "dynamic_groups": [], "modes": {}}

    def _file_sig(self) -> tuple | None:
        try:
            st = self._path.stat()
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None

    def _load(self) -> dict:
        """Return the store doc, using an mtime+size cache to avoid re-parsing."""
        with self._lock:
            sig = self._file_sig()
            if sig is not None and sig == self._cache_sig and self._cache is not None:
                return self._cache
            if sig is None:
                self._cache, self._cache_sig = self._default_doc(), None
                return self._cache
            try:
                doc = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(doc, dict):
                    raise ValueError("store root is not an object")
            except Exception as exc:
                logger.warning("signal-group-chat: store unreadable (%s); using defaults", exc)
                doc = self._default_doc()
            base = self._default_doc()
            base.update(doc)
            self._cache, self._cache_sig = base, sig
            return base

    def _save(self, doc: dict) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self._path)
            self._cache, self._cache_sig = doc, self._file_sig()

    def _mutate(self, fn) -> Any:
        with self._lock:
            doc = json.loads(json.dumps(self._load()))  # deep copy
            result = fn(doc)
            self._save(doc)
            return result

    # -- one-time migration --------------------------------------------------

    def _ensure_seeded(self) -> None:
        if self._file_sig() is not None:
            return
        doc = self._default_doc()
        # Only migrate from the production legacy files for the real store path;
        # a custom path (tests / alternate profiles) starts empty.
        if self._path != STORE_PATH:
            try:
                self._save(doc)
            except Exception as exc:
                logger.warning("signal-group-chat: could not write store: %s", exc)
            return
        try:
            if _LEGACY_APPROVED.exists():
                legacy = json.loads(_LEGACY_APPROVED.read_text(encoding="utf-8"))
                if isinstance(legacy, dict):
                    doc["approved"] = legacy.get("groups", {}) or {}
                    doc["global_approved"] = legacy.get("global", []) or []
            if _LEGACY_DYNAMIC_GROUPS.exists():
                legacy = json.loads(_LEGACY_DYNAMIC_GROUPS.read_text(encoding="utf-8"))
                if isinstance(legacy, list):
                    doc["dynamic_groups"] = [str(g) for g in legacy if g]
            if _LEGACY_MODES.exists():
                legacy = json.loads(_LEGACY_MODES.read_text(encoding="utf-8"))
                if isinstance(legacy, dict):
                    doc["modes"] = legacy
        except Exception as exc:
            logger.warning("signal-group-chat: legacy seed failed: %s", exc)
        try:
            self._save(doc)
            logger.info("signal-group-chat: initialized store at %s", self._path)
        except Exception as exc:
            logger.warning("signal-group-chat: could not write store: %s", exc)

    # -- approvals -----------------------------------------------------------

    def is_approved(self, sender: str, group_id: str | None) -> bool:
        doc = self._load()
        if group_id and sender in doc.get("approved", {}).get(group_id, []):
            return True
        return sender in doc.get("global_approved", [])

    def approve(self, sender: str, group_id: str) -> bool:
        """Add *sender* to a group's approved list. Returns False if already present."""
        def _fn(doc):
            lst = doc.setdefault("approved", {}).setdefault(group_id, [])
            if sender in lst:
                return False
            lst.append(sender)
            return True
        return self._mutate(_fn)

    def revoke(self, sender: str, group_id: str) -> bool:
        """Remove *sender* from a group's approved list. Returns False if absent."""
        def _fn(doc):
            lst = doc.setdefault("approved", {}).setdefault(group_id, [])
            if sender not in lst:
                return False
            lst.remove(sender)
            return True
        return self._mutate(_fn)

    def approved_members(self, group_id: str) -> list[str]:
        return list(self._load().get("approved", {}).get(group_id, []))

    # -- dynamic groups ------------------------------------------------------

    def dynamic_groups(self) -> list[str]:
        return list(self._load().get("dynamic_groups", []))

    def add_dynamic_group(self, group_id: str) -> None:
        def _fn(doc):
            lst = doc.setdefault("dynamic_groups", [])
            if group_id not in lst:
                lst.append(group_id)
        self._mutate(_fn)

    # -- modes ---------------------------------------------------------------

    def get_mode(self, group_id: str, default: str = "single") -> str:
        entry = self._load().get("modes", {}).get(group_id) or {}
        mode = entry.get("mode")
        return mode if mode in _VALID_MODES else default

    def set_mode(self, group_id: str, mode: str) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"invalid mode {mode!r}")

        def _fn(doc):
            doc.setdefault("modes", {}).setdefault(group_id, {})["mode"] = mode
        self._mutate(_fn)

    def get_wake_words(self, group_id: str) -> list[str]:
        entry = self._load().get("modes", {}).get(group_id) or {}
        words = entry.get("wake_words") or []
        return [str(w) for w in words if w]

    def add_wake_word(self, group_id: str, word: str) -> bool:
        word = (word or "").strip().lower()
        if not word:
            return False

        def _fn(doc):
            entry = doc.setdefault("modes", {}).setdefault(group_id, {})
            words = entry.setdefault("wake_words", [])
            if word in words:
                return False
            words.append(word)
            return True
        return self._mutate(_fn)

    def remove_wake_word(self, group_id: str, word: str) -> bool:
        word = (word or "").strip().lower()

        def _fn(doc):
            entry = doc.setdefault("modes", {}).setdefault(group_id, {})
            words = entry.setdefault("wake_words", [])
            if word not in words:
                return False
            words.remove(word)
            return True
        return self._mutate(_fn)
