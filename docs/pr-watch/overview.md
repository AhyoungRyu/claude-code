# PR Watch Overview

`pr-watch` keeps PR attention signals close to the local agent session that can act on them. It does not run a hosted webhook service and it does not let an agent inspect code or GitHub updates without user approval.

## How It Works

| Step | Component | What happens |
|------|-----------|--------------|
| 1 | Poller | Reads watched GitHub repositories through local `gh` auth |
| 2 | Classifier | Converts PR changes into deduped inbox events |
| 3 | Session discovery | Scans local Claude/Codex history for PR URLs, repo anchors, branches, cwd, activity, and host hints |
| 4 | Binding router | Reuses confirmed PR/session bindings or asks for confirmation |
| 5 | Notification layer | Sends desktop, in-app, or Conductor-visible notification prompts |
| 6 | User approval | Queues or resumes a session only after approval |

## State Model

| Item | Location | Purpose |
|------|----------|---------|
| Config | `~/.pr-watch/config.toml` | Defaults for notifications, polling, drafts, busy behavior, and event filters |
| Database | `~/.pr-watch/state.sqlite` | Inbox events, bindings, queue items, notifications, host sync records |
| Logs | `~/.pr-watch/logs/` | launchd service output |
| Icon | `~/.pr-watch/pr-watch-notification.png` | PR Watch desktop notification icon |

Use `--state-dir` to isolate all of those paths for testing.

## Event Lifecycle

| Status | Meaning |
|--------|---------|
| `pending` | Event is recorded and waiting for a decision |
| `needs_confirmation` | PR Watch found a likely session but needs the user to confirm it |
| `awaiting_approval` | Binding is confirmed and delivery still needs approval |
| `queued` | Delivery is queued because the target session should not be interrupted |
| `delivered` | Delivery was approved and attempted |
| `dismissed` | User dismissed or marked the event handled |

Notification prompts are deliberately separate from delivery. A desktop notification or Conductor-visible prompt means "this PR needs attention"; it does not mean the agent has inspected the PR, edited files, posted comments, or resumed a session.

## Session Binding Rules

| Case | Behavior |
|------|----------|
| First likely match | Ask the user to confirm the session |
| Confirmed binding exists | Reuse it for the same PR and role |
| Multiple confirmed sessions | Keep history, but only the newest confirmed active binding is the delivery target for that PR and role |
| New high-confidence candidate | Ask whether to make it the active handler |
| Ambiguous candidates | Leave the event pending for manual choice |
| Low-confidence candidate | Avoid session-visible prompt; keep inbox decision |
| PR no longer open | Deactivate its active bindings during repo polling |
| Legacy duplicate active bindings | Deactivate older confirmed bindings during repo polling |

When several local sessions match the same PR, PR Watch prefers active/focused host sessions when available, then Conductor sessions over CLI/meta sessions at the same confidence, then stronger PR evidence, then newest `last_activity_at`.

## Event Sources

PR Watch records actionable context from:

| Source | Examples |
|--------|----------|
| PR timeline | review requested, requested changes, approval, author push after review |
| Checks | CI failure and merge conflict signals |
| PR comments | human top-level comments |
| Review comments | human inline review comments |
| Linked issues | comments on issues referenced by the PR |

Bot/service noise from deploy preview accounts such as GitHub Actions, Dependabot, Renovate, Netlify, and Vercel is ignored where applicable.
