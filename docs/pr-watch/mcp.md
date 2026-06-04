# PR Watch MCP

PR Watch can run as a stdio MCP server. Codex App, Codex CLI, and Conductor can share the same local state directory.

## Register

```bash
pr-watch install-mcp --target all
pr-watch install-mcp --target codex-app
pr-watch install-mcp --target conductor
pr-watch install-mcp --target all --dry-run
```

Advanced options:

```bash
pr-watch --state-dir ~/.pr-watch-test install-mcp --target all
pr-watch install-mcp --target all --python /path/to/python3.11
pr-watch install-mcp --target all --no-replace
pr-watch install-mcp --target all --codex-bin ~/bin/codex
```

For other MCP hosts, run the server directly:

```bash
pr-watch mcp
pr-watch-mcp
```

## What Registration Does

| Action | Result |
|--------|--------|
| Adds `pr-watch` MCP entry | Uses the detected `codex mcp add` command |
| Launch command | `python -m pr_watch --state-dir ~/.pr-watch mcp` |
| Codex App/CLI target | Uses `codex` from `PATH` or `~/bin/codex` |
| Conductor target | Uses Conductor's bundled Codex binary when present |
| Restart requirement | Start a new host session after registration |

`install-mcp` only registers tools. It does not add repositories to the watch allowlist and it does not install the background service.

After registration, add repositories explicitly. From a host session in the repo, ask PR Watch to use `watch_current_repo`. From a terminal, run:

```bash
pr-watch setup --current-repo
pr-watch watch add owner/name
```

## Usage Examples

Ask a host session:

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

Watch the current repository from an MCP host session:

```json
{
  "tool": "watch_current_repo",
  "arguments": {
    "cwd": "/path/to/repo"
  }
}
```

Check watch coverage:

```json
{
  "tool": "list_watched_repos",
  "arguments": {}
}
```

Approve a session resume or queue:

```json
{
  "tool": "approve_resume_session",
  "arguments": {
    "event_id": "evt_123",
    "session_state": "unknown"
  }
}
```

Confirm a candidate session without delivery:

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

Confirm and dismiss an already-handled update:

```json
{
  "tool": "confirm_binding_and_mark_handled",
  "arguments": {
    "event_id": "evt_123"
  }
}
```

Reject or dismiss:

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

## Tool Map

| Tool | Use |
|------|-----|
| `check_pr_updates` | Poll GitHub once and record actionable PR events |
| `show_pending_pr_actions` | Show events waiting for user approval or binding |
| `confirm_binding_for_event` | Confirm or reassign the active PR/session binding without delivery |
| `confirm_binding_and_mark_handled` | Confirm the binding and dismiss the current event |
| `reject_binding_for_event` | Reject the inferred session candidate while keeping the event pending |
| `dismiss_event` | Dismiss one event without inspecting the PR |
| `approve_resume_session` | Approve delivery to the matched session |
| `queue_resume_session` | Queue delivery without trying to run immediately |
| `notify_prompt_session` | Send a guardrailed notification-only prompt to a confirmed bound session |
| `show_in_app_notifications` | Show app-hosted notification inbox items |
| `ack_notification` | Mark an in-app notification as read |
| `bind_pr` | Explicitly bind a PR to a Claude or Codex session |
| `list_watched_repos` | List repositories polled by the background service |
| `watch_repo` | Add an explicit `owner/name` repository to background polling |
| `watch_current_repo` | Detect the GitHub remote for the current working directory and watch it |
| `doctor` | Report dependency, config, auth, and watch allowlist status |
