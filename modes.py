"""Per-group mode management and summon detection for /group mode.

Modes:
  - "single": Hermes replies to every authorized message (legacy behavior).
  - "group" : Hermes records messages but stays silent until *summoned*.

A message summons Hermes when ANY of these is true:
  1. it starts with the summon keyword (default ``/agent``)
  2. it @mentions the bot (Signal mention matching our account, or "@<bot name>")
  3. it is a reply/quote of one of the bot's own messages
  4. it contains a configured wake-word (whole-word, case-insensitive)
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ._util import normalize_token
from .config_store import ConfigStore, plugin_setting


class ModeManager:
    def __init__(self, store: ConfigStore):
        self._store = store

    # -- mode state (delegates to the backup-aware store) --------------------

    def default_mode(self) -> str:
        mode = str(plugin_setting("default_mode", "single", env="SIGNALGC_DEFAULT_MODE")).strip().lower()
        return mode if mode in {"single", "group"} else "single"

    def get_mode(self, group_id: str) -> str:
        return self._store.get_mode(group_id, default=self.default_mode())

    def set_mode(self, group_id: str, mode: str) -> None:
        self._store.set_mode(group_id, mode)

    def summon_keyword(self) -> str:
        return str(plugin_setting("summon_keyword", "/agent", env="SIGNALGC_SUMMON_KEYWORD")).strip() or "/agent"

    def bot_name(self) -> str:
        return str(plugin_setting("bot_name", "", env="SIGNALGC_BOT_NAME")).strip()

    def wake_words(self, group_id: str) -> list[str]:
        words = list(self._store.get_wake_words(group_id))
        configured = plugin_setting("wake_words", None)
        if isinstance(configured, str):
            words += [w.strip().lower() for w in configured.split(",") if w.strip()]
        elif isinstance(configured, (list, tuple)):
            words += [str(w).strip().lower() for w in configured if str(w).strip()]
        return list(dict.fromkeys(words))

    # -- summon detection ----------------------------------------------------

    def is_summon(
        self,
        group_id: str,
        *,
        text: str,
        mentions: list | None = None,
        quote: dict | None = None,
        account_ids: Iterable[str] | None = None,
    ) -> tuple[bool, str]:
        """Return (summoned, cleaned_text). cleaned_text strips a leading keyword."""
        body = text or ""
        keyword = self.summon_keyword()

        # 1. keyword prefix (e.g. "/agent what's up")
        if body.strip().lower().startswith(keyword.lower()):
            cleaned = body.strip()[len(keyword):].lstrip(" :,-").strip()
            return True, cleaned or body.strip()

        ids = {normalize_token(a) for a in (account_ids or []) if a}

        # 2. @mention of the bot
        for m in mentions or []:
            if not isinstance(m, dict):
                continue
            if normalize_token(m.get("number")) in ids or normalize_token(m.get("uuid")) in ids:
                return True, body.strip()
        name = self.bot_name()
        if name and re.search(rf"(?<!\w)@?{re.escape(name)}(?!\w)", body, re.IGNORECASE):
            return True, body.strip()

        # 3. reply/quote of one of the bot's own messages
        if isinstance(quote, dict):
            q_author = normalize_token(
                quote.get("author") or quote.get("authorNumber") or quote.get("authorUuid")
            )
            if q_author and q_author in ids:
                return True, body.strip()

        # 4. wake-word (whole word, case-insensitive)
        low = body.lower()
        for w in self.wake_words(group_id):
            if w and re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low):
                return True, body.strip()

        return False, body.strip()

    def compose_summon_prompt(self, transcript: str, summon_text: str, asker: str) -> str:
        """Build the augmented prompt the agent sees when summoned in /group mode."""
        parts: list[str] = []
        if transcript.strip():
            parts.append(
                "You are in a Signal group chat in GROUP mode: you have been listening "
                "silently. Here is the recent group conversation for context:\n"
                "<group_transcript>\n" + transcript.strip() + "\n</group_transcript>"
            )
        who = asker or "A group member"
        parts.append(f"{who} has now summoned you. Respond to their request:\n{summon_text}".strip())
        return "\n\n".join(parts)
