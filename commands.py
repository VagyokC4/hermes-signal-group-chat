"""Owner/admin in-chat command handling (typed in a group by the owner).

Returns a response string when a command is handled, or None to let the message
continue through normal processing. Security-approval keywords (bare ``/approve``,
``/approve always|session|once|cancel``) are deliberately NOT handled here so the
core dangerous-command approval flow still works.
"""

from __future__ import annotations

from ._util import signal_directory_entries, signal_entry_chat_id
from .config_store import is_valid_identifier
from .observability import audit, count, snapshot

_SECURITY_KEYWORDS = {"always", "session", "once", "cancel", "deny"}

_HELP = (
    "Hermes group commands (owner only):\n"
    "/mode single|group — set how I behave here\n"
    "/agent <msg> — summon me (group mode)\n"
    "/approve <id> · /revoke <id> — manage members\n"
    "/wake add|remove|list <word> — wake-words\n"
    "/delete-watch add|remove *|here|<room> · list — recover deleted messages\n"
    "/status — current mode & stats\n"
    "/forget — clear my memory of recent chat\n"
    "/help — this message"
)

# command prefixes we intercept (lowercased, first token)
COMMAND_PREFIXES = (
    "/mode", "/status", "/forget", "/wake", "/help", "/approve", "/revoke",
    "/delete-watch", "/watch",
)


def _resolve_room(ref: str, current_group_id: str) -> str | None:
    """Resolve a /delete-watch target to a chat_id, '*', or None.

    Accepts '*' (all), 'here' (this room), an explicit chat_id (group:… / +num),
    a bare group id (base64), or a room name (looked up in the channel directory).
    """
    ref = (ref or "").strip()
    if ref in ("", "list"):
        return None
    if ref == "*":
        return "*"
    if ref.lower() == "here":
        return f"group:{current_group_id}" if current_group_id else None
    if ref.startswith("group:") or ref.startswith("+"):
        return ref
    for e in signal_directory_entries(ref):
        cid = signal_entry_chat_id(e)
        if cid:
            return cid
    # Looks like a bare group id (base64) — prefix it.
    if ref.endswith("=") and "/" not in ref[:1]:
        return f"group:{ref}"
    return ref


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

    if cmd in ("/delete-watch", "/watch"):
        sub = arg.split(None, 1)
        action = sub[0].lower() if sub else ""
        ref = sub[1].strip() if len(sub) > 1 else ""
        if action in ("", "list", "status"):
            all_on = store.delete_watch_all()
            rooms = store.delete_watch_rooms()
            s = adapter.delete_cache_stats()
            scope = "ALL rooms" if all_on else (f"{len(rooms)} room(s)" if rooms else "OFF (no rooms watched)")

            def _age(mins):
                return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"

            lines = [
                "🗑️ Delete-watch status",
                f"Scope: {scope}",
                f"Retention: {s['ttl_hours']}h (Signal delete window 24h + 2h buffer)",
                f"Cached: {s['rows']} msg(s) across {s['rooms']} room(s) "
                f"(cap {s['max_rows']}, db {s['db_bytes'] // 1024} KB)",
                f"Fresh: {s['fresh']} · Stale/pending-prune: {s['stale']}",
                f"Oldest: {_age(s['oldest_age_min'])} ago · Newest: {_age(s['newest_age_min'])} ago"
                if s["rows"] else "Oldest/Newest: —",
                f"Deletes recovered/seen: {s['processed_events']}",
            ]
            if all_on and s["top_rooms"]:
                lines.append("Top rooms: " + ", ".join(f"{lbl} ({c})" for lbl, c in s["top_rooms"]))
            elif rooms and not all_on:
                lines.append("Watching: " + ", ".join((r[:24] + "…") if len(r) > 24 else r for r in rooms))
            return "\n".join(lines)
        if action in ("add", "on", "enable"):
            target = _resolve_room(ref, group_id)
            if not target:
                return "Usage: /delete-watch add *|here|<room name or id>"
            ok = store.delete_watch_add(target)
            audit("delete_watch_add", group=group_id, actor=sender, target=target)
            label = "all rooms" if target == "*" else target
            return f"🗑️ Now watching {label} for deleted messages." if ok else f"Already watching {label}."
        if action in ("remove", "off", "disable", "rm"):
            target = _resolve_room(ref, group_id)
            if not target:
                return "Usage: /delete-watch remove *|here|<room name or id>"
            ok = store.delete_watch_remove(target)
            audit("delete_watch_remove", group=group_id, actor=sender, target=target)
            label = "all rooms" if target == "*" else target
            return f"Stopped watching {label}." if ok else f"{label} wasn't being watched."
        return "Usage: /delete-watch add|remove *|here|<room> · /delete-watch list"

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
