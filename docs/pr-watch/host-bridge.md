# PR Watch Host Bridge

The host bridge surfaces already-recorded pending events in desktop/app hosts. It does not patch app binaries and it does not connect to Conductor sidecar sockets.

## Commands

Check support:

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

Enable host sync through the background service:

```bash
pr-watch setup --current-repo --install-service --host-sync --host conductor
pr-watch service install --interval 120 --notification-mode auto --host-sync --host conductor
```

## Confirmation Flow

| User action | Command | Result |
|-------------|---------|--------|
| Confirm session | `pr-watch confirm-binding evt_123` | Adds or updates the active PR/session binding |
| Confirm and mark handled | `pr-watch confirm-binding evt_123 --mark-handled` | Confirms binding and dismisses only that update |
| Reject session | `pr-watch reject-binding evt_123` | Marks the candidate as wrong and keeps the event pending |
| Ignore update | `pr-watch dismiss-event evt_123` | Closes the single event without PR action |
| Approve delivery | `pr-watch approve evt_123 --session-state unknown` | Queues or resumes according to session state and busy policy |

`confirm-binding` does not resume or queue the session unless `--trigger` is passed.

## Host Surfaces

| Surface | What it does | What it does not do |
|---------|--------------|---------------------|
| Desktop notification | Sends a local macOS notification for polling or notify commands | Does not approve, resume, queue, or update app unread state |
| In-app MCP inbox | Stores durable local notifications readable through MCP tools | Does not make Codex App show an automatic badge or popup by itself |
| Conductor confirmation prompt | Inserts a guardrailed visible synthetic turn into the matched Conductor session when possible | Does not inspect files, call GitHub, edit code, comment, push, or execute work |
| Notify prompt soft trigger | Sends a guardrailed notification-only prompt asking whether to inspect the PR update | Does not mark the event delivered or take external action |
| Conductor mirror | Writes visible synthetic PR Watch turns into Conductor's local SQLite surface | Does not execute the session or use Conductor's sidecar socket |
| Confirmed-binding auto-trigger | With `--trigger-confirmed`, approves/queues/resumes confirmed high-confidence bindings | Does not auto-confirm first inferred bindings |

## Conductor Behavior

When Conductor host sync sees a high-confidence candidate that needs confirmation, it inserts a visible notification-only synthetic turn into the matched Conductor session if the Conductor SQLite surface is available.

If the DB surface is unavailable, PR Watch falls back to the bundled `codex exec resume` notification prompt path.

Visible host prompts are one-shot per event/session. Unanswered prompts remain pending and inbox-only until the user explicitly confirms, rejects, dismisses, or approves them.

PR Watch avoids inserting full session-visible prompts while a Conductor session is actively working. When the matched Conductor session status is `working` or another busy state, host sync sends only the desktop notification and returns `deferred_session_busy`; it does not write a synthetic turn or mark the host sync as complete. A later host sync inserts the session prompt once the session is idle.

If the matched Conductor session already has an explicit PR work command for the same PR after the event was recorded, such as `/review-pr https://github.com/owner/repo/pull/123`, host sync treats that event as already handled. For an unconfirmed candidate it confirms the binding, dismisses only that event with `handled_by_session_activity`, and avoids inserting a redundant prompt. Events recorded after that review command still prompt normally.

Conductor prompts include concise suggested replies:

| Prompt type | Suggested replies |
|-------------|-------------------|
| Confirmation | `Confirm this session`, `Confirm and mark handled`, `Not this session`, `Ignore this update` |
| Confirmed update | `Inspect update`, `Queue for later`, `Ignore this update` |

PR Watch stores best-effort `suggested_replies` metadata for hosts that render clickable replies and includes the same choices as plain text for hosts that do not.

## Codex App Status

MCP registration lets Codex App call PR Watch inbox tools. There is no known stable local Codex App API for automatic app badges, popups, or session pushes, so Codex App support is currently diagnostic for push-style UI.

## Experimental Surface

The Conductor mirror writes to Conductor's private local SQLite schema. It is the current best-effort path for session-visible Conductor notifications because Conductor renders its own `session_messages` rows, not Codex JSONL rows written by an external `codex` process.

Synthetic turns are visible notifications rather than executable queued prompts. Use `pr-watch host status` first when diagnosing this surface.
