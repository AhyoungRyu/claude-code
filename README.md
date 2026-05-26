# Agent Workflow Tools

Local agent workflow tooling, including `pr-watch` and a small set of Claude Code skills.

`pr-watch` is not a Claude Code skill. It is a Python CLI and MCP server that can work with Claude Code, Codex, Codex App, and Conductor sessions.

## Start Here

| Need | Go here |
|------|---------|
| Install PR Watch for the first time | [Quick Start](#quick-start) |
| Decide repo-level vs user-level setup | [Install Scope](#install-scope) |
| Configure noisy event types or notification channels | [Core Config](#core-config) |
| Connect Codex App, Codex CLI, or Conductor through MCP | [MCP setup](./docs/pr-watch/mcp.md) |
| Understand Conductor/session-visible prompts | [Host bridge](./docs/pr-watch/host-bridge.md) |
| Debug missing or duplicate notifications | [Troubleshooting](./docs/pr-watch/troubleshooting.md) |
| Install only the bundled Claude Code skills | [Bundled Skills](#bundled-skills) |

## PR Watch

`pr-watch` polls GitHub with your existing `gh` login, records PR events in `~/.pr-watch/`, finds likely Claude/Codex sessions, and asks before resuming or queueing work.

It is intentionally local-first:

| Property | Behavior |
|----------|----------|
| State | SQLite and config under `~/.pr-watch/` by default |
| GitHub access | Uses local `gh` authentication |
| Polling | Explicit watched-repository allowlist |
| Delivery | First inferred binding requires confirmation |
| Sessions | Supports multiple active handlers for the same PR and role |
| Notifications | Desktop, local in-app inbox, and Conductor-visible prompts are notification-only |

Detailed behavior: [PR Watch overview](./docs/pr-watch/overview.md).

## Quick Start

Install from this checkout with Python 3.11+:

```bash
python3.11 -m pip install -e .
pr-watch doctor
```

Initialize defaults for your main host:

```bash
pr-watch init --profile conductor
```

Register MCP tools for Codex App/CLI and Conductor:

```bash
pr-watch install-mcp --target all
```

Enable background polling for the current repository:

```bash
pr-watch setup --current-repo --install-service --host-sync --host conductor --notify-prompt-confirmed
```

Inspect pending work:

```bash
pr-watch inbox
pr-watch notifications
pr-watch host status
```

More setup paths: [setup guide](./docs/pr-watch/setup.md).

## Install Scope

| Setup | Scope | What it affects |
|-------|-------|-----------------|
| `pip install -e .` | Current Python environment | Makes `pr-watch` CLI available from that environment |
| `pr-watch install-mcp --target ...` | User-level for the selected host | Makes MCP tools available in Codex App/CLI or Conductor sessions across repositories |
| `pr-watch setup --current-repo` | One repository | Adds the current GitHub remote to the watched-repository allowlist |
| `pr-watch setup --repo owner/name` | One repository | Adds that repo to the allowlist |
| `pr-watch service install` | User-level launchd service | Polls only repositories in the local allowlist |
| `--state-dir /path/to/state` | Isolated local state | Uses a separate DB/config for testing or private setups |

In practice: MCP registration is user-level, but background PR polling is limited to repositories you explicitly add with `setup` or `watch add`.

## Core Config

Config lives in `~/.pr-watch/config.toml` unless you pass `--state-dir`.

```bash
pr-watch config set notification_mode auto
pr-watch config set busy_policy run_if_idle_queue_if_busy
pr-watch config set include_drafts false
pr-watch config set poll_interval_seconds 120
pr-watch config set notify_event_types 'author_push_after_review,review_requested,human_comment,human_review_comment,linked_issue_comment'
```

| Option | Default | Values | Notes |
|--------|---------|--------|-------|
| `notification_mode` | `auto` | `auto`, `none`, `desktop`, `in_app`, `both` | Notification only; does not approve, resume, or queue work |
| `notify_event_types` | `["*"]` | TOML array, comma list, or `["*"]` | Filters automatic notifications; inbox events are still recorded |
| `busy_policy` | `run_if_idle_queue_if_busy` | `run_if_idle_queue_if_busy`, `always_queue`, `notify_only`, `drop_if_busy`, `ask_when_busy` | Used after you approve delivery |
| `default_delivery` | `confirm_first` | `confirm_first` | First inferred bindings ask for confirmation |
| `include_drafts` | `false` | `true`, `false` | Draft PRs are ignored unless enabled |
| `poll_interval_seconds` | `120` | integer seconds | Used by service install and daemon loop |
| `--state-dir` | `~/.pr-watch` | path | Use isolated state/config for tests |

Notification event types are configured as an array:

```toml
notify_event_types = [
  "author_push_after_review",
  "review_requested",
  "human_comment",
  "human_review_comment",
  "linked_issue_comment",
]
```

Supported events and tuning examples: [configuration](./docs/pr-watch/configuration.md).

## Common Commands

| Task | Command |
|------|---------|
| Add current repo to background polling | `pr-watch setup --current-repo --install-service` |
| Add a specific repo | `pr-watch setup --repo owner/name --install-service` |
| Add repo without touching service | `pr-watch watch add owner/name` |
| List watched repos | `pr-watch watch list` |
| Poll once | `pr-watch daemon --once --repo owner/name` |
| Show inbox | `pr-watch inbox` |
| Confirm a candidate session | `pr-watch confirm-binding evt_123` |
| Confirm and dismiss already-handled update | `pr-watch confirm-binding evt_123 --mark-handled` |
| Reject the suggested session | `pr-watch reject-binding evt_123` |
| Dismiss one event | `pr-watch dismiss-event evt_123` |
| Approve delivery | `pr-watch approve evt_123 --session-state unknown` |
| Sync pending host prompts once | `pr-watch host sync-once --host conductor --notify-prompt-confirmed` |
| Service status | `pr-watch service status` |

## PR Watch Docs

| Document | Contents |
|----------|----------|
| [Overview](./docs/pr-watch/overview.md) | Local-first model, event lifecycle, session matching |
| [Setup](./docs/pr-watch/setup.md) | Install, MCP registration, repo allowlist, launchd service |
| [MCP](./docs/pr-watch/mcp.md) | MCP tools, registration targets, usage examples |
| [Host Bridge](./docs/pr-watch/host-bridge.md) | Conductor/Codex host sync, visible prompts, limitations |
| [Configuration](./docs/pr-watch/configuration.md) | Defaults, notification modes, event filters |
| [Troubleshooting](./docs/pr-watch/troubleshooting.md) | Duplicate, missing, stale, and Script Editor notifications |

## Bundled Skills

| Skill | Description |
|-------|-------------|
| [playbook](./skills/playbook/) | Turn a goal into a structured, executable runbook |
| [review-pr](./skills/review-pr/) | Comprehensive PR review with parallel specialist agents |
| [review-pr-comments](./skills/review-pr-comments/) | Process GitHub PR review comments and post responses |

## Install Bundled Skills Only

```bash
git clone https://github.com/AhyoungRyu/claude-code.git
cp -r claude-code/skills/<skill-name> ~/.claude/skills/
cp -r claude-code/skills/* ~/.claude/skills/
```

Then invoke in Claude Code:

```text
/playbook <your goal>
/review-pr [PR number | branch]
/review-pr-comments
```
