# hermes-signal-group-chat

A production Signal platform plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

It **subclasses the upstream Signal adapter** (inheriting upstream fixes — no forking of `signal.py`) and registers itself as platform `signal`, overriding the built-in adapter. All existing `SIGNAL_*` environment variables keep working.

## Features

- **`/single` and `/group` modes** (per group, runtime-switchable):
  - **single** — Hermes replies to every authorized message (classic behavior).
  - **group** — Hermes listens silently and only engages when **summoned**, then answers with the recent group conversation as context.
- **Summon triggers** (group mode): the `/agent` keyword, an **@mention**, a **reply** to one of Hermes's messages, or a configurable **wake-word**.
- **Access control** — owner-only DMs; per-group guest **`/approve`** / **`/revoke`** with a two-message owner notification flow.
- **Backup-aware security state** — approvals, dynamic groups, and per-group modes are stored in `/opt/data/platforms/pairing/signal-group-chat.json` so they ride Hermes's pre-update snapshots **and** full backups. Layered config: runtime store → `config.yaml` (`plugins.entries.signal-group-chat`) → `SIGNAL_*` env (bootstrap). Atomic writes, mtime-cached reads, `0600` perms.
- **Deleted-message watch** — recovers remote-deleted messages from a watched contact/room to an alert chat.
- **File staging** — auto-copies attachments into the shared `/opt/data` volume so signal-cli can deliver them (covers both adapter sends and the `send_message` tool).
- **Owner admin commands** (in-group): `/mode`, `/status`, `/forget`, `/wake add|remove|list`, `/help`, plus `/approve` / `/revoke`.
- **Observability** — append-only audit log (`/opt/data/signal-audit.jsonl`) of approvals/revokes/mode-changes/summons, plus per-group counters surfaced by `/status`.

## Install

```bash
hermes plugins install VagyokC4/hermes-signal-group-chat --enable
# set SIGNAL_HTTP_URL and SIGNAL_ACCOUNT (or accept the prompts), then restart the gateway
```

The plugin is opt-in via `plugins.enabled` in `config.yaml` (the `--enable` flag adds it). It clones into `~/.hermes/plugins/` (`= $HERMES_HOME/plugins`).

## Configuration

| Env var | Purpose |
|---|---|
| `SIGNAL_HTTP_URL`, `SIGNAL_ACCOUNT` | required — signal-cli daemon URL + account |
| `SIGNAL_OWNER_USERS` | owner identifiers (default: `SIGNAL_ACCOUNT`) |
| `SIGNAL_GROUP_ALLOWED_USERS` | group allowlist (`*` = all) |
| `SIGNAL_HOME_CHANNEL` | admin channel for access-request notices |
| `SIGNALGC_DEFAULT_MODE` | `single` (default) or `group` |
| `SIGNALGC_SUMMON_KEYWORD` | summon prefix (default `/agent`) |
| `SIGNALGC_BOT_NAME` | name for @mention summon detection |

Declarative policy can also be set under `plugins.entries.signal-group-chat` in `config.yaml` (e.g. `default_mode`, `summon_keyword`, `bot_name`, `wake_words`).

## Group-mode commands (owner)

```
/mode group              # listen silently; respond only when summoned
/mode single             # respond to everything
/agent <question>        # summon in group mode
/wake add hermes         # add a wake-word
/status                  # mode, buffered messages, approvals, counters
/forget                  # clear the recent-conversation buffer
/approve +15551234567    # grant a guest access in this group
/revoke +15551234567
```

## Development

```bash
python -m pytest          # 21 tests; runs in the Hermes venv (gateway on PYTHONPATH)
ruff check .
```

Iterate against a live instance: edit, commit, push, then
`hermes plugins install VagyokC4/hermes-signal-group-chat --force && restart gateway`.

## License

MIT
