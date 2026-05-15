# Claude Code Skills

Personal collection of Claude Code skills for productivity automation.

## PR Watch

This repository includes a local-first PR session watcher CLI, `pr-watch`.
It polls GitHub with the user's existing `gh` authentication, stores state under
`~/.pr-watch/`, tracks PR/session bindings, and queues or resumes Claude Code and
Codex sessions only after user approval.

### Quick Start

Install from this checkout with Python 3.11+:

```bash
python3.11 -m pip install -e .
```

Check dependencies and create a default profile:

```bash
pr-watch doctor
pr-watch init --profile app        # Codex App / Conductor / MCP users
# or
pr-watch init --profile terminal   # plain terminal users
```

Poll one repository once:

```bash
pr-watch daemon --once --repo owner/name
pr-watch inbox
```

Watch repositories continuously through a user-level macOS service:

```bash
pr-watch setup --current-repo --install-service
# or
pr-watch setup --repo owner/name --install-service
pr-watch service status
```

The service is a launchd `StartInterval` job, not a resident Python process. It
wakes up, runs `pr-watch service run-once`, polls the watched repositories,
updates `~/.pr-watch/state.sqlite`, emits configured notifications, then exits.
The MCP server and CLI use the same `~/.pr-watch` state, so Codex App,
Conductor, and terminal sessions see the same inbox and notification history.

Approve an event before it resumes or queues a session:

```bash
pr-watch approve <event-id> --session-state unknown
pr-watch queue
```

### MCP Setup

`pr-watch` can run as a stdio MCP server. Codex App, Codex CLI, and Conductor
can all share the same local watcher state in `~/.pr-watch`.

Register the MCP server:

```bash
pr-watch install-mcp --target all        # Codex App/CLI and Conductor
pr-watch install-mcp --target codex-app  # Codex App/CLI only
pr-watch install-mcp --target conductor  # Conductor only
pr-watch install-mcp --target all --dry-run
```

`install-mcp` writes a `pr-watch` MCP entry through the detected `codex mcp add`
command. The Codex App/CLI target uses `codex` from `PATH` or `~/bin/codex`;
the Conductor target uses Conductor's bundled Codex binary at
`~/Library/Application Support/com.conductor.app/bin/codex` when present. The
registered server launches the current Python environment with
`python -m pr_watch --state-dir ~/.pr-watch mcp`, so app-hosted sessions and
terminal sessions share the same watcher state. Restart Codex App or start a
new Conductor/Codex session after installation so the host reloads MCP config.
`install-mcp` only registers tools; it does not add watched repositories or
install the background service.

For the background watcher flow, keep MCP registered and run setup from the
repository you want to watch:

```bash
pr-watch setup --current-repo --install-service
```

`setup --current-repo` detects the GitHub `owner/name` from the current git
remote, adds it to the explicit watch allowlist, and installs or updates the
launchd service only when `--install-service` is present. launchd detects PR
updates in the background, while MCP remains the approval, resume, queue, and
in-app notification interface. Background notifications do not approve, resume,
or queue sessions by themselves. Each repository poll also reconciles the live
open PR list and dismisses stale pending inbox items for PRs that have been
merged, closed, or otherwise disappeared from that repo's open PRs.

### Host Bridge

The host bridge is an opt-in local adapter layer for surfacing already-recorded
pending events in desktop/app hosts. It does not patch app binaries and does not
connect to Conductor sidecar sockets.

Check host support:

```bash
pr-watch host status
pr-watch host status --conductor-db "$HOME/Library/Application Support/com.conductor.app/conductor.db"
```

Sync pending events once:

```bash
pr-watch host sync-once --host conductor
pr-watch host sync-once --host conductor --session-state idle
pr-watch host sync-once --host conductor --trigger-confirmed
pr-watch host sync-once --host conductor --notify-prompt-confirmed --session-state idle
pr-watch host sync-once --host codex-app --notify-prompt-confirmed --session-state idle
```

Confirm an inferred or rebind candidate without approving delivery:

```bash
pr-watch confirm-binding evt_123 --no-mirror
pr-watch confirm-binding evt_123 --conductor-db "$HOME/Library/Application Support/com.conductor.app/conductor.db"
```

Reject a wrong candidate or dismiss one notification:

```bash
pr-watch reject-binding evt_123
pr-watch dismiss-event evt_123
```

`confirm-binding` updates the active PR/session binding and returns the event to
`pending` / `awaiting_approval` at high confidence. By default it mirrors the
current event to Conductor immediately when a Conductor DB is available. It does
not resume or queue the session unless `--trigger` is passed. Use `approve`
when you are ready to deliver work to the confirmed session.
`reject-binding` marks only the inferred session candidate as wrong and leaves
the event pending for another match. `dismiss-event` closes that single PR Watch
event without inspecting the PR or taking GitHub action.

When Conductor host sync sees a high-confidence inferred or rebind candidate
that still needs confirmation, it inserts a visible notification-only synthetic
turn into the matched Conductor session when the Conductor SQLite surface is
available. The synthetic turn has a native user row plus assistant row sharing a
`turn_id`, so Conductor renders it in the transcript and marks the session
unread. If the Conductor DB is unavailable, PR Watch falls back to the bundled
`codex exec resume` notification prompt path. The prompt is separate from the
final PR update, so confirming the event can still mirror the actual update
afterward. Visible host prompts are one-shot per event/session; unanswered
prompts remain pending and inbox-only until the user explicitly confirms,
rejects, dismisses, or approves them.

Conductor prompts include concise suggested replies. Confirmation prompts offer
`Confirm this session`, `Not this session`, and `Ignore this update`; confirmed
update prompts offer `Inspect update`, `Queue for later`, and
`Ignore this update`. PR Watch stores best-effort `suggested_replies` metadata
for hosts that render clickable replies, and also includes the same choices as
plain text so Codex, Claude Code, and terminal sessions still work when the host
does not render native buttons.

The same bridge is exposed through MCP as `host_status` and `sync_host_once`
for hosts that can explicitly call MCP tools. These tools still do not make the
host app subscribe to PR Watch automatically; they provide the bridge action
when the host or agent chooses to call it.

Host surfaces are intentionally different:

| Surface | What it does | What it does not do |
|---------|--------------|---------------------|
| Desktop notification | Sends a local macOS notification when polling or `notify` runs | Does not approve, resume, queue, or update app unread state |
| In-app MCP inbox | Stores durable `in_app` notifications readable through MCP tools | Does not make Codex App show an automatic badge or popup by itself |
| Conductor confirmation prompt | For `needs_confirmation` candidates, inserts a guardrailed notification-only synthetic turn into the matched Conductor session; falls back to bundled `codex exec resume` only when the DB surface is unavailable | Does not confirm the binding, mark the event delivered, inspect files, call GitHub, edit code, comment, push, or take external actions |
| Notify prompt soft trigger | With `notify-prompt` or `--notify-prompt-confirmed`, sends a guardrailed notification-only prompt asking whether to inspect the PR update. Conductor host sync prefers the visible SQLite turn; non-Conductor targets use resume/queue. | Does not mark the event delivered, inspect files, call GitHub, edit code, comment, push, or take external actions |
| Conductor mirror | Experimental/private-surface SQLite adapter that inserts visible synthetic PR Watch turns into the matched Conductor session and marks its session/workspace unread | Does not execute the session or use Conductor's sidecar socket |
| Confirmed-binding auto-trigger | With `--trigger-confirmed`, approves/queues/resumes only confirmed, high-confidence bindings | Does not auto-confirm first inferred bindings; ambiguous or low-confidence events stay pending |

Codex App support is currently diagnostic only for push-style UI. MCP
registration lets Codex App call the PR Watch inbox tools, but there is no known
stable local Codex App API for automatic app badges, popups, or session pushes.

The Conductor mirror is marked experimental because it writes to Conductor's
private local SQLite schema. It is the current best-effort path for
session-visible Conductor notifications because Conductor renders its own
`session_messages` rows, not Codex JSONL rows written by an external `codex`
process. Synthetic turns are inserted without host queue metadata, so they are
visible notifications rather than executable queued prompts. Use `host status`
first when diagnosing the private DB surface.
Mirroring is deduped per event/host target and ignores legacy hidden synthetic
rows that lack a visible `turn_id`, so upgrading PR Watch can repair earlier
hidden notifications without spamming once a visible turn exists. It also
repairs older visible PR Watch turns that predate the suggested-reply format.

To make host sync automatic, reuse the existing launchd one-shot watcher and add
`--host-sync`:

```bash
pr-watch setup --current-repo --install-service --host-sync --host conductor
pr-watch service install --interval 120 --notification-mode in_app --host-sync --host conductor
```

With `--host conductor`, high-confidence `needs_confirmation` events use the
notification-only visible Conductor turn path when the Conductor DB is
available. If the DB is unavailable, PR Watch falls back to the Conductor Codex
resume prompt path. The service path defaults that fallback prompt's session
state to `idle`.

Add `--trigger-confirmed` only when you want confirmed bindings to be queued or
resumed automatically. The default session state is `unknown`, so confirmed
events queue by default from the service path. The service installer does not
pretend to detect whether a host session is idle. For an explicit manual run
that may invoke `claude --resume` or `codex exec resume`, use:

```bash
pr-watch host sync-once --trigger-confirmed --session-state idle
```

Use `--notify-prompt-confirmed` when you want confirmed events surfaced as
notification-only prompts in the bound session. This leaves the PR event
`pending` / `awaiting_approval`; for Conductor, host sync writes a visible
synthetic turn when the DB surface is available and uses the resume/queue prompt
path only as fallback. The service path defaults this fallback soft prompt
session state to `idle`; pass `--notify-prompt-session-state unknown` if you
prefer queue-only behavior when fallback is needed. You can also target one
event manually:

```bash
pr-watch notify-prompt <event-id> --session-state unknown
pr-watch host sync-once --host codex-app --notify-prompt-confirmed --session-state idle
pr-watch service install --host-sync --host codex-app --notify-prompt-confirmed --notify-prompt-session-state idle
```

Advanced registration options:

```bash
pr-watch --state-dir ~/.pr-watch-test install-mcp --target all
pr-watch install-mcp --target all --python /path/to/python3.11
pr-watch install-mcp --target all --no-replace
pr-watch install-mcp --target all --codex-bin ~/bin/codex
```

For any other MCP host, run the server directly:

```bash
pr-watch mcp
# or
pr-watch-mcp
```

### MCP Usage Examples

After registration, ask Codex App or Conductor to use the `pr-watch` MCP tools:

```text
Check PR updates for AhyoungRyu/claude-code with notification_mode=auto.
```

Equivalent MCP tool call:

```json
{
  "tool": "check_pr_updates",
  "arguments": {
    "repo": "AhyoungRyu/claude-code",
    "notification_mode": "auto"
  }
}
```

Review pending actions:

```json
{
  "tool": "show_pending_pr_actions",
  "arguments": {}
}
```

Approve a resume after reviewing the notification:

```json
{
  "tool": "approve_resume_session",
  "arguments": {
    "event_id": "evt_123",
    "session_state": "unknown"
  }
}
```

Confirm an inferred binding or active-handler rebind without delivery:

```json
{
  "tool": "confirm_binding_for_event",
  "arguments": {
    "event_id": "evt_123",
    "mirror_now": true,
    "trigger": false
  }
}
```

Reject a wrong session candidate or dismiss a single event:

```json
{
  "tool": "reject_binding_for_event",
  "arguments": {
    "event_id": "evt_123"
  }
}
```

```json
{
  "tool": "dismiss_event",
  "arguments": {
    "event_id": "evt_123"
  }
}
```

Show and acknowledge in-app notifications:

```json
{
  "tool": "show_in_app_notifications",
  "arguments": {}
}
```

```json
{
  "tool": "ack_notification",
  "arguments": {
    "notification_id": "note_123"
  }
}
```

Useful MCP tools:

| Tool | Use |
|------|-----|
| `check_pr_updates` | Poll GitHub once and record actionable PR events |
| `show_pending_pr_actions` | Show events waiting for user approval or binding |
| `confirm_binding_for_event` | Confirm or reassign the active PR/session binding without delivery |
| `reject_binding_for_event` | Reject the inferred session candidate while keeping the event pending |
| `dismiss_event` | Dismiss one event without inspecting the PR or taking GitHub action |
| `approve_resume_session` | Approve delivery to the matched session |
| `queue_resume_session` | Queue delivery without trying to run immediately |
| `notify_prompt_session` | Send a guardrailed notification-only prompt to the confirmed bound session |
| `show_in_app_notifications` | Show app-hosted notification inbox items |
| `ack_notification` | Mark an in-app notification as read |
| `bind_pr` | Explicitly bind a PR to a Claude or Codex session |
| `doctor` | Report dependency, config, and auth status |

### Options

Notifications are independent from resume/queue. Set `notification_mode` to
`auto`, `none`, `desktop`, `in_app`, or `both` to fan out notifications when
polling:

```bash
pr-watch init --profile terminal      # desktop notifications for CLI users
pr-watch init --profile conductor     # in-app inbox for Conductor hosts
pr-watch init --profile app           # in-app inbox for app/MCP hosts
pr-watch daemon --once --repo owner/name --notification-mode desktop
pr-watch service install --interval 120 --notification-mode in_app
pr-watch service run-once --notification-mode none
pr-watch notify <event-id> --mode in_app
pr-watch notifications
```

The default `auto` mode resolves to `in_app` for app-style hosts such as MCP,
Conductor, or Codex App adapters, and to `desktop` for plain macOS terminal
usage. The `desktop` channel uses the local macOS notification bridge. The
`in_app` channel writes a durable local notification inbox that MCP, Codex App,
or Conductor adapters can consume without approving, resuming, or queueing the
agent session. The old `browser` value is still accepted as a legacy alias for
`in_app`.

Common configuration:

```bash
pr-watch config set notification_mode auto
pr-watch config set busy_policy run_if_idle_queue_if_busy
pr-watch config set include_drafts false
pr-watch config set poll_interval_seconds 120
```

| Option | Values | Default | Notes |
|--------|--------|---------|-------|
| `notification_mode` | `auto`, `none`, `desktop`, `in_app`, `both` | `auto` | Notification only; does not resume or queue sessions |
| `busy_policy` | `run_if_idle_queue_if_busy`, `always_queue`, `notify_only`, `drop_if_busy`, `ask_when_busy` | `run_if_idle_queue_if_busy` | Used when approving delivery |
| `include_drafts` | `true`, `false` | `false` | Draft PRs are ignored unless enabled |
| `poll_interval_seconds` | integer seconds | `120` | Used by `service install --interval` and the long-running daemon |
| `--state-dir` | path | `~/.pr-watch` | Use a separate state/config directory for tests |

### Background Watcher

Background polling uses an explicit repository allowlist. The easiest setup path
is:

```bash
pr-watch setup --current-repo --install-service
pr-watch setup --repo owner/name --install-service
```

Use `--notification-mode`, `--interval`, `--state-dir`, and `--dry-run` with
`setup` when you want to preview or customize the service install. `setup` adds
the repository first and installs the service only when `--install-service` is
present. `setup --interactive` is intentionally small: it prompts for the repo
and whether to install the service, then uses the same defaults as the
non-interactive command.

Manual watch control remains available for power users:

```bash
pr-watch watch add owner/name
pr-watch watch remove owner/name
pr-watch watch list
pr-watch watch clear
```

Manage the macOS user service:

```bash
pr-watch service install --interval 120 --notification-mode auto --target macos-launchd
pr-watch service status
pr-watch service uninstall
```

`service install` writes a LaunchAgent plist under `~/Library/LaunchAgents/`
and logs under `~/.pr-watch/logs/`. Each launchd wake-up invokes
`pr-watch service run-once`, which takes a single-worker lock in the state
directory. If a previous poll is still active, the overlapping run exits
cleanly without doing duplicate work.

`service run-once` loads watched repositories from local state and polls them
sequentially. The service detects PR updates; MCP handles approval, resume,
queue, and inbox workflows. It preserves the same dedupe, inbox status,
notification, and approval semantics as `daemon --once`, so manual CLI/MCP
polling continues to work exactly as before. Notifications are currently sent
per event and deduped per event/channel; batching is intentionally deferred so
the service stays small and the existing event-level recovery semantics remain
unchanged.

By default, `pr-watch` ignores draft pull requests and only records events for
PRs that are ready for review. Draft PRs usually do not have meaningful review
or status transitions, so watching them is opt-in:

```bash
pr-watch daemon --once --repo owner/name --include-drafts
pr-watch config set include_drafts true
```

For local fixture replay while developing:

```bash
pr-watch daemon --once --fixture tests/fixtures/prs.json --user <github-login>
```

The MVP deliberately does not depend on Conductor internals or any shared
webhook service. Low-confidence events stay in the inbox, first inferred
bindings require approval, and unknown session state is treated as busy so
approved work queues by default.

Confirmed bindings are mutable active-handler pointers. When a new high
confidence session candidate appears for the same PR and role, `pr-watch` keeps
the old binding active but marks the event `needs_confirmation` /
`awaiting_rebind_confirmation` for the new candidate. Confirming that candidate
supersedes the previous binding for future events. Ambiguous or low-confidence
different candidates stay as manual inbox decisions and are not mirrored to the
old active session for that event.

When several local sessions match the same PR, `pr-watch` prefers
active/focused host sessions when that signal is available, then stronger PR
evidence, then the newest `last_activity_at`. If multiple medium-or-better
candidates are still tied, the event stays in the inbox with
`ambiguous_session_candidates` until the user explicitly chooses one session.

`pr-watch` also treats inline PR review comments and comments on GitHub issues
linked from a PR as actionable PR context. It fetches PR review comments plus
GitHub's `closingIssuesReferences` and issue references in the PR body with
`gh api`, then stores new human inline review comments as
`human_review_comment` events and linked issue comments as
`linked_issue_comment` inbox events. PR, inline review, and linked issue comments
from bot/service deploy preview accounts such as Netlify, Vercel, GitHub
Actions, Dependabot, and Renovate are ignored so they do not become confirmation
prompts. Jira ticket updates are the same product shape, but need a separate
authenticated Jira adapter before they can be polled safely.

## Skills

| Skill | Description |
|-------|-------------|
| [playbook](./skills/playbook/) | Turn any goal into a structured, executable runbook — classifies task type, generates a Codex-authored plan, executes autonomously |
| [review-pr](./skills/review-pr/) | Comprehensive PR review — initializes playbook artifacts, routes to parallel specialist agents (code, security, quality, performance, API), aggregates severity-rated report, applies local fixes |
| [review-pr-comments](./skills/review-pr-comments/) | Process GitHub PR review comments — generates action plans, addresses feedback, posts responses back to reviewers |

## Install

```bash
git clone https://github.com/AhyoungRyu/claude-code.git

# Install a specific skill
cp -r claude-code/skills/<skill-name> ~/.claude/skills/

# Install all skills
cp -r claude-code/skills/* ~/.claude/skills/
```

Then invoke in Claude Code:
```
/playbook <your goal>
/review-pr [PR number | branch]
/review-pr-comments
```
