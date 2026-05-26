# PR Watch Troubleshooting

## Quick Checks

```bash
pr-watch doctor
pr-watch service status
pr-watch watch list
pr-watch inbox
pr-watch notifications --all
pr-watch host status
```

## Missing Conductor Prompt

| Check | Why |
|-------|-----|
| `pr-watch host status` | Confirms whether the Conductor DB surface is available |
| `pr-watch inbox` | Confirms whether the event exists and is still pending |
| `pr-watch notifications --all` | Shows desktop/in-app notification attempts and failures |
| `pr-watch host sync-once --host conductor --notify-prompt-confirmed --session-state idle` | Manually retries host sync |

Common causes:

| Symptom | Likely cause |
|---------|--------------|
| Desktop notification appeared, no session prompt | The event only went through desktop notification, or Conductor DB sync was unavailable |
| Prompt appears late | Conductor rendered an older unread/synthetic row after UI refresh |
| No prompt while a session is running | PR Watch deferred the session-visible prompt because Conductor reported the session as `working`; it should retry on a later idle host sync |
| No prompt after starting `/review-pr` | PR Watch found explicit same-PR work in the bound Conductor session after the event time and marked that event handled |
| Prompt appears in old session | Existing confirmed binding pointed to that session; confirm/reject the binding to teach PR Watch |
| No prompt for a new PR | Repo may not be in `watch list`, MCP only may be installed without background polling, or event type was filtered from notifications |

## Script Editor Notification

Script Editor notifications come from the macOS `osascript` fallback. They are non-clickable and cannot control click behavior.

For clickable PR Watch notifications with the PR Watch icon:

```bash
brew install terminal-notifier
```

Then send or wait for a new notification. Conductor-bound desktop notifications should open `conductor://open`; other events open the GitHub PR URL.

## Duplicate Notifications

PR Watch dedupes per event/channel. Duplicates can still happen when:

| Case | Explanation |
|------|-------------|
| Distinct events have similar summaries | Example: review request plus author push on the same PR |
| Desktop and Conductor prompt both fire | Desktop notification and session-visible prompt are separate surfaces |
| Old failed notification is retried | Failed sends are allowed to retry |
| State was changed during testing | Old pending events can surface after service or host sync is re-enabled |
| Same PR appears bound to old sessions | Repo polling keeps only the newest confirmed active binding per PR and role; older bindings remain as inactive history |

Use:

```bash
pr-watch inbox --all
pr-watch notifications --all
```

Then dismiss one event if it is no longer useful:

```bash
pr-watch dismiss-event evt_123
```

## Stale PR Events

Each repository poll reconciles live open PRs, dismisses stale pending inbox items for PRs that disappeared from the open PR list, and deactivates active bindings for those closed or merged PRs. Historical events and notifications remain in the local DB.

If an already-merged PR still appears:

```bash
pr-watch daemon --once --repo owner/name --notification-mode none
pr-watch inbox --all
```

## No Automatic Notifications

Check these config values:

```bash
pr-watch doctor
```

| Setting | Effect |
|---------|--------|
| `notification_mode = "none"` | Records events but sends no automatic notifications |
| `notify_event_types = []` | Records events but sends no event notifications |
| Missing watched repo | Background service has nothing to poll |
| Missing service | Events only appear when MCP/CLI polling is run manually |

## Local Fixture Replay

For development:

```bash
pr-watch daemon --once --fixture tests/fixtures/prs.json --user <github-login>
```

Use `--state-dir ~/.pr-watch-test` when replaying fixtures to avoid touching your normal inbox.
