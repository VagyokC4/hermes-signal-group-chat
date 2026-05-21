"""Owner/admin in-chat command handling (typed in a group by the owner).

Returns a response string when a command is handled, or None to let the message
continue through normal processing. Security-approval keywords (bare ``/approve``,
``/approve always|session|once|cancel``) are deliberately NOT handled here so the
core dangerous-command approval flow still works.
"""

from __future__ import annotations

from .config_store import is_valid_identifier
from .observability import audit, count, snapshot

_SECURITY_KEYWORDS = {"always", "session", "once", "cancel", "deny"}

_HELP = (
    "Hermes group commands (owner only):\n"
    "/mode single|group — set how I behave here\n"
    "/agent <msg> — summon me (group mode)\n"
    "/approve <id> · /revoke <id> — manage members\n"
    "/wake add|remove|list <word> — wake-words\n"
    "/status — current mode & stats\n"
    "/forget — clear my memory of recent chat\n"
    "/help — this message"
)

# command prefixes we intercept (lowercased, first token)
COMMAND_PREFIXES = ("/mode", "/status", "/forget", "/wake", "/help", "/approve", "/revoke")


def looks_like_command(text: str) -> bool:
    if not text:
        return False
    return text.strip().lower().split(None, 1)[0] in COMMAND_PREFIXES


async def handle_owner_command(adapter, text: str, group_id: str, sender: str) -> str | None:
    stripped = (text or "").strip()
    parts = stripped.split(None, 1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    store = adapter._store
    modes = adapter._modes
    buffer = adapter._buffer

    if cmd == "/help":
        return _HELP

    if cmd == "/mode":
        if not arg:
            return f"Mode here is *{modes.get_mode(group_id)}*. Use /mode single or /mode group."
        new = arg.split()[0].lower()
        if new not in {"single", "group"}:
            return "Usage: /mode single|group"
        modes.set_mode(group_id, new)
        audit("mode_changed", group=group_id, actor=sender, mode=new)
        if new == "group":
            return ("Switched to *group* mode. I'll stay quiet and only respond when "
                    "summoned (/agent, @mention, a reply to me, or a wake-word).")
        return "Switched to *single* mode. I'll respond to every message here."

    if cmd == "/status":
        c = snapshot(group_id)
        return (
            f"Mode: {modes.get_mode(group_id)}\n"
            f"Buffered messages: {buffer.size(group_id)}\n"
            f"Approved members: {len(store.approved_members(group_id))}\n"
            f"Wake-words: {', '.join(modes.wake_words(group_id)) or '(none)'}\n"
            f"Seen: {c.get('seen', 0)} · Responded: {c.get('responded', 0)}"
        )

    if cmd == "/forget":
        buffer.clear(group_id)
        audit("buffer_cleared", group=group_id, actor=sender)
        return "Cleared my memory of the recent conversation in this group."

    if cmd == "/wake":
        sub = arg.split(None, 1)
        action = sub[0].lower() if sub else ""
        word = sub[1].strip() if len(sub) > 1 else ""
        if action == "list" or not action:
            return f"Wake-words: {', '.join(modes.wake_words(group_id)) or '(none)'}"
        if action == "add" and word:
            ok = store.add_wake_word(group_id, word)
            audit("wake_add", group=group_id, actor=sender, word=word)
            return f"Added wake-word '{word.lower()}'." if ok else f"'{word.lower()}' already set."
        if action == "remove" and word:
            ok = store.remove_wake_word(group_id, word)
            audit("wake_remove", group=group_id, actor=sender, word=word)
            return f"Removed wake-word '{word.lower()}'." if ok else f"'{word.lower()}' wasn't set."
        return "Usage: /wake add|remove|list <word>"

    if cmd in ("/approve", "/revoke"):
        # Leave bare / security-keyword forms to the core approval flow.
        if not arg or arg.split()[0].lower() in _SECURITY_KEYWORDS:
            return None
        target = arg.split()[0]
        if not is_valid_identifier(target):
            return f"'{target}' doesn't look like a phone number (+1...) or Signal ID."
        if cmd == "/approve":
            ok = store.approve(target, group_id)
            audit("approve", group=group_id, actor=sender, target=target)
            count(group_id, "approvals")
            return (f"✅ {target} approved. They can now chat with me in this group."
                    if ok else f"{target} is already approved.")
        ok = store.revoke(target, group_id)
        audit("revoke", group=group_id, actor=sender, target=target)
        return (f"❌ {target} revoked. They can no longer chat with me in this group."
                if ok else f"{target} was not in the approved list.")

    return None
