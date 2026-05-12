# Claude Code Skills

Personal collection of Claude Code skills for productivity automation.

## PR Watch

This repository includes a local-first PR session watcher CLI, `pr-watch`.
It polls GitHub with the user's existing `gh` authentication, stores state under
`~/.pr-watch/`, tracks PR/session bindings, and queues or resumes Claude Code and
Codex sessions only after user approval.

Install from this checkout with Python 3.11+:

```bash
python3.11 -m pip install -e .
```

Useful commands:

```bash
pr-watch doctor
pr-watch daemon --once --repo owner/name
pr-watch inbox
pr-watch bind https://github.com/owner/name/pull/1049 --role reviewer --agent codex --session-id <session-id>
pr-watch approve <event-id>
pr-watch queue
pr-watch init --profile terminal
pr-watch config set busy_policy run_if_idle_queue_if_busy
pr-watch config set notification_mode auto
pr-watch mcp
```

For local fixture replay while developing:

```bash
pr-watch daemon --once --fixture tests/fixtures/prs.json --user <github-login>
```

By default, `pr-watch` ignores draft pull requests and only records events for
PRs that are ready for review. Draft PRs usually do not have meaningful review
or status transitions, so watching them is opt-in:

```bash
pr-watch daemon --once --repo owner/name --include-drafts
pr-watch config set include_drafts true
```

For MCP clients, run the local stdio server with:

```bash
pr-watch mcp
# or
pr-watch-mcp
```

The MCP server exposes the same local state and approval flow as the CLI:
`poll_once`, `list_inbox`, `bind_pr`, `approve`, `notify`,
`list_notifications`, `list_queue`, and `doctor`. It also exposes user-facing
aliases such as `check_pr_updates`, `show_pending_pr_actions`,
`approve_resume_session`, `queue_resume_session`, `show_in_app_notifications`,
and `ack_notification`.

Notifications are independent from resume/queue. Set `notification_mode` to
`auto`, `none`, `desktop`, `in_app`, or `both` to fan out notifications when
polling:

```bash
pr-watch init --profile terminal      # desktop notifications for CLI users
pr-watch init --profile conductor     # in-app inbox for Conductor hosts
pr-watch init --profile app           # in-app inbox for app/MCP hosts
pr-watch daemon --once --repo owner/name --notification-mode desktop
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

The MVP deliberately does not depend on Conductor internals or any shared
webhook service. Low-confidence events stay in the inbox, first inferred
bindings require approval, and unknown session state is treated as busy so
approved work queues by default.

`pr-watch` also treats comments on GitHub issues linked from a PR as actionable
PR context. It follows GitHub's `closingIssuesReferences` plus issue references
in the PR body, fetches issue comments with `gh api`, and stores new human
comments as `linked_issue_comment` inbox events. Jira ticket updates are the
same product shape, but need a separate authenticated Jira adapter before they
can be polled safely.

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
