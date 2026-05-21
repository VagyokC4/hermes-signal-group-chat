"""Plugin entry point — wires the platform adapter and the send_message override."""

from __future__ import annotations

from .observability import logger

_PLATFORM_HINT = (
    "You are talking over Signal. In a group set to GROUP mode you only see a "
    "message when you've been summoned (via /agent, an @mention, a reply to one "
    "of your messages, or a wake-word); a recent group transcript is provided for "
    "context. In SINGLE mode you reply to every message. Owner-only group commands: "
    "/mode single|group, /approve <id>, /revoke <id>, /wake add|remove|list <word>, "
    "/status, /forget, /help."
)


def register(ctx) -> None:
    from gateway.platforms.signal import check_signal_requirements

    from .adapter import SignalGroupChatAdapter

    ctx.register_platform(
        name="signal",
        label="Signal (Group Chat)",
        adapter_factory=lambda cfg: SignalGroupChatAdapter(cfg),
        check_fn=check_signal_requirements,
        required_env=["SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT"],
        install_hint="Set SIGNAL_HTTP_URL and SIGNAL_ACCOUNT; run signal-cli in daemon --http mode.",
        allowed_users_env="SIGNAL_ALLOWED_USERS",
        allow_all_env="SIGNAL_ALLOW_ALL_USERS",
        cron_deliver_env_var="SIGNAL_HOME_CHANNEL",
        pii_safe=True,
        emoji="💬",
        platform_hint=_PLATFORM_HINT,
    )

    # send_message override (staging + attachments + session routing). Optional:
    # if the upstream tool's internals ever change, the platform still loads.
    try:
        from .tools import register_tools

        register_tools(ctx)
    except Exception as exc:
        logger.warning("signal-group-chat: send_message override not installed: %s", exc)

    logger.info("signal-group-chat plugin registered (platform=signal)")
