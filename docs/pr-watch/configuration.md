# PR Watch Configuration

Config lives in `~/.pr-watch/config.toml` by default.

## Defaults

| Option | Default | Values | Notes |
|--------|---------|--------|-------|
| `notification_mode` | `auto` | `auto`, `none`, `desktop`, `in_app`, `both` | Controls notification fan-out only |
| `notify_event_types` | `["*"]` | TOML array, comma list, or `["*"]` | Filters automatic notifications only |
| `busy_policy` | `run_if_idle_queue_if_busy` | `run_if_idle_queue_if_busy`, `always_queue`, `notify_only`, `drop_if_busy`, `ask_when_busy` | Used after delivery approval |
| `default_delivery` | `confirm_first` | `confirm_first` | First inferred binding needs user confirmation |
| `include_drafts` | `false` | `true`, `false` | Draft PRs are skipped unless enabled |
| `poll_interval_seconds` | `120` | integer seconds | Used by daemon loop and service setup |
| `--state-dir` | `~/.pr-watch` | path | Isolates config, DB, logs, notifications, and icon |

## Set Values

```bash
pr-watch config set notification_mode auto
pr-watch config set busy_policy run_if_idle_queue_if_busy
pr-watch config set include_drafts false
pr-watch config set poll_interval_seconds 120
```

List values can be set with comma-separated input:

```bash
pr-watch config set notify_event_types 'author_push_after_review,review_requested,human_comment'
```

Or as TOML arrays:

```bash
pr-watch config set notify_event_types '["author_push_after_review", "review_requested", "human_comment"]'
```

## Notification Modes

| Mode | Behavior |
|------|----------|
| `auto` | Uses `desktop` for plain macOS/Conductor desktop paths and `in_app` for app/MCP hosts |
| `none` | Records events without automatic notifications |
| `desktop` | Sends macOS desktop notifications |
| `in_app` | Writes durable local notification inbox items |
| `both` | Sends desktop and in-app notifications |
| `browser` | Legacy alias for `in_app` |

The `desktop` channel uses `terminal-notifier` when available. Clickable PR Watch notifications use the generated PR Watch icon. Conductor-bound events focus the Conductor app; other events open the GitHub PR URL. Conductor-bound notifications are sent with the local `PR Watch.app` sender identity so they appear as PR Watch notifications, not as generic terminal notifications. The `osascript` fallback is non-clickable.

## Notification Event Types

`notify_event_types` controls which events create automatic notifications. It does not stop events from being recorded in the inbox.

Examples:

```toml
notify_event_types = ["*"]
notify_event_types = []
notify_event_types = ["author_push_after_review", "review_requested"]
notify_event_types = ["human_comment", "human_review_comment", "linked_issue_comment"]
```

| Value | Meaning |
|-------|---------|
| `["*"]` | Notify for all supported event types |
| `[]` | Record events but send no automatic event notifications |
| Explicit array | Notify only for listed event types |

Supported event types:

| Event type | Typical role | Meaning |
|------------|--------------|---------|
| `author_push_after_review` | Reviewer | The PR author pushed commits after your review |
| `author_reply` | Reviewer | The PR author replied in review context |
| `review_requested` | Requested reviewer | You were requested as a reviewer |
| `thread_resolved` | Reviewer | A review thread was resolved |
| `thread_reopened` | Reviewer | A review thread was reopened |
| `requested_changes` | Author | Someone requested changes on your PR |
| `ci_failed` | Author | One or more checks failed on your PR |
| `merge_conflict` | Author | Your PR has a merge conflict |
| `human_comment` | Author | A human left a top-level PR comment |
| `human_review_comment` | Author | A human left inline PR review comments |
| `linked_issue_comment` | Author or reviewer | A human commented on an issue linked from the PR |

## Draft PRs

Draft PRs are ignored by default.

```bash
pr-watch config set include_drafts true
pr-watch daemon --once --repo owner/name --include-drafts
```

## Busy Policy

`busy_policy` applies only after you approve delivery.

| Policy | Behavior |
|--------|----------|
| `run_if_idle_queue_if_busy` | Run immediately when idle; queue when busy or unknown |
| `always_queue` | Always add to queue |
| `notify_only` | Notify but do not run or queue |
| `drop_if_busy` | Run only when idle; drop otherwise |
| `ask_when_busy` | Ask when the session appears busy |
