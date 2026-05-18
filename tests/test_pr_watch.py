import os
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from pr_watch.classifier import classify_pr
from pr_watch.cli import main
from pr_watch.config import load_config
from pr_watch.delivery import CommandResult, RecordingRunner, approve_event
from pr_watch.github import GH_PR_FIELDS, enrich_pull_request_metadata, poll_once
from pr_watch.host_integration import (
    RecordingHostCommandRunner,
    build_codex_mcp_add_command,
    build_mcp_launch_config,
    install_mcp_hosts,
)
from pr_watch.models import ClassifiedEvent, PullRequestRef, SessionInfo
from pr_watch.notifications import resolve_notification_mode
from pr_watch.setup import detect_current_repo, parse_github_remote_url
from pr_watch.sessions import discover_sessions
from pr_watch.state import StateStore
from pr_watch.util import stable_id
from pr_watch.workflow import create_explicit_binding, route_event


PR_URL = "https://github.com/sendbird/ai-agent-js/pull/1049"


def make_store(tmpdir):
    return StateStore(Path(tmpdir) / "state.sqlite")


def make_review_event(dedupe_suffix="main"):
    return ClassifiedEvent(
        pr=PullRequestRef(
            owner="sendbird",
            repo="ai-agent-js",
            number=1049,
            url=PR_URL,
            title="Improve the tool runner",
            head_ref="review/pr-1049",
        ),
        role="reviewer",
        event_type="author_push_after_review",
        summary="bang9 pushed new commits to PR #1049",
        actor="bang9",
        actionable=True,
        dedupe_key=f"test:1049:{dedupe_suffix}",
        payload={"lastPushedAt": "2026-05-11T10:00:00Z"},
    )


def make_inbox_event(owner, repo, number, dedupe_suffix, role="reviewer", event_type="author_push_after_review"):
    return ClassifiedEvent(
        pr=PullRequestRef(
            owner=owner,
            repo=repo,
            number=number,
            url=f"https://github.com/{owner}/{repo}/pull/{number}",
            title=f"PR {number}",
            head_ref=f"pr-{number}",
        ),
        role=role,
        event_type=event_type,
        summary=f"PR #{number} needs attention",
        actor="bang9",
        actionable=True,
        dedupe_key=f"test:{owner}/{repo}:{number}:{dedupe_suffix}",
        payload={"lastPushedAt": "2026-05-11T10:00:00Z"},
    )


class PrWatchTests(unittest.TestCase):
    def test_github_polling_requests_pr_comments(self):
        self.assertIn("comments", GH_PR_FIELDS.split(","))
        self.assertIn("mergeable", GH_PR_FIELDS.split(","))

    def test_github_polling_requests_draft_status(self):
        self.assertIn("isDraft", GH_PR_FIELDS.split(","))

    def test_github_polling_requests_commits_for_reviewer_push_detection(self):
        self.assertIn("commits", GH_PR_FIELDS.split(","))

    def test_github_polling_derives_last_pushed_at_from_commits(self):
        prs = [
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "commits": [
                    {"committedDate": "2026-05-11T09:00:00Z"},
                    {"committedDate": "2026-05-11T10:30:00Z"},
                ],
            }
        ]

        with patch("pr_watch.github.fetch_pull_request_review_comments", return_value=[]):
            enrich_pull_request_metadata(prs, "sendbird/ai-agent-js")

        self.assertEqual("2026-05-11T10:30:00Z", prs[0]["lastPushedAt"])

    def test_github_polling_enriches_inline_review_comments(self):
        prs = [
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1061,
                "commits": [],
            }
        ]

        def fake_run(args, capture_output, text, check):
            self.assertEqual(
                ["gh", "api", "repos/sendbird/ai-agent-js/pulls/1061/comments"],
                args,
            )
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="""
                [
                  {
                    "id": 2313318077,
                    "html_url": "https://github.com/sendbird/ai-agent-js/pull/1061#discussion_r2313318077",
                    "user": {"login": "bang9", "type": "User"},
                    "author_association": "MEMBER",
                    "body": "Can we simplify this branch?",
                    "path": "src/runtime.ts",
                    "created_at": "2026-05-15T02:42:33Z",
                    "updated_at": "2026-05-15T02:42:33Z"
                  }
                ]
                """,
                stderr="",
            )

        with patch("pr_watch.github.subprocess.run", fake_run):
            enrich_pull_request_metadata(prs, "sendbird/ai-agent-js")

        self.assertEqual(
            [
                {
                    "id": 2313318077,
                    "url": "https://github.com/sendbird/ai-agent-js/pull/1061#discussion_r2313318077",
                    "author": {"login": "bang9", "type": "User"},
                    "authorAssociation": "MEMBER",
                    "body": "Can we simplify this branch?",
                    "path": "src/runtime.ts",
                    "createdAt": "2026-05-15T02:42:33Z",
                    "updatedAt": "2026-05-15T02:42:33Z",
                }
            ],
            prs[0]["reviewComments"],
        )

    def test_auto_notification_mode_prefers_in_app_for_app_hosts(self):
        self.assertEqual(
            "in_app",
            resolve_notification_mode("auto", host="conductor", platform_name="Darwin"),
        )
        self.assertEqual(
            "in_app",
            resolve_notification_mode("auto", host="codex_app", platform_name="Darwin"),
        )
        self.assertEqual(
            "in_app",
            resolve_notification_mode("auto", host="codex-app", platform_name="Darwin"),
        )

    def test_auto_notification_mode_uses_desktop_for_plain_macos_terminal(self):
        self.assertEqual("desktop", resolve_notification_mode("auto", platform_name="Darwin"))

    def test_browser_notification_mode_is_legacy_alias_for_in_app(self):
        self.assertEqual("in_app", resolve_notification_mode("browser", platform_name="Darwin"))

    def test_reviewer_update_summary_uses_github_login(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "url": PR_URL,
                "title": "Improve the tool runner",
                "author": {"login": "bang9"},
                "latestReviews": [
                    {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z",
                    }
                ],
                "lastPushedAt": "2026-05-11T10:00:00Z",
                "updatedAt": "2026-05-11T10:00:00Z",
            },
            current_user="irene",
        )

        event = next(item for item in events if item.event_type == "author_push_after_review")
        self.assertEqual("bang9", event.actor)
        self.assertEqual("bang9 pushed commits to PR #1049 after your review.", event.summary)
        self.assertNotIn("teammate", event.summary)

    def test_reviewer_author_replies_are_coalesced_per_actor(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "url": PR_URL,
                "title": "Improve the tool runner",
                "author": {"login": "bang9"},
                "latestReviews": [
                    {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z",
                    }
                ],
                "authorReplies": [
                    {
                        "id": "reply-1",
                        "author": {"login": "bang9"},
                        "body": "Fixed the first part.",
                        "updatedAt": "2026-05-11T10:00:00Z",
                    },
                    {
                        "id": "reply-2",
                        "author": {"login": "bang9"},
                        "body": "Added the test too.",
                        "updatedAt": "2026-05-11T10:03:00Z",
                    },
                    {
                        "id": "reply-3",
                        "author": {"login": "bang9"},
                        "body": "Ready for another look.",
                        "updatedAt": "2026-05-11T10:05:00Z",
                    },
                ],
                "updatedAt": "2026-05-11T10:05:00Z",
            },
            current_user="irene",
        )

        reply_events = [event for event in events if event.event_type == "author_reply"]

        self.assertEqual(1, len(reply_events))
        self.assertEqual("bang9 replied 3 times on PR #1049.", reply_events[0].summary)
        self.assertEqual(["reply-1", "reply-2", "reply-3"], reply_events[0].payload["reply_ids"])
        self.assertEqual("reply-3", reply_events[0].payload["reply_id"])
        self.assertEqual("author_reply:bang9", reply_events[0].payload["condition_key"])

    def test_reviewer_thread_state_changes_are_coalesced_per_state_and_actor(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "url": PR_URL,
                "title": "Improve the tool runner",
                "author": {"login": "bang9"},
                "latestReviews": [
                    {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z",
                    }
                ],
                "reviewThreads": [
                    {
                        "id": "thread-1",
                        "state": "resolved",
                        "actor": {"login": "bang9"},
                        "updatedAt": "2026-05-11T10:00:00Z",
                    },
                    {
                        "id": "thread-2",
                        "state": "resolved",
                        "actor": {"login": "bang9"},
                        "updatedAt": "2026-05-11T10:03:00Z",
                    },
                    {
                        "id": "thread-3",
                        "state": "reopened",
                        "actor": {"login": "bang9"},
                        "updatedAt": "2026-05-11T10:05:00Z",
                    },
                ],
                "updatedAt": "2026-05-11T10:05:00Z",
            },
            current_user="irene",
        )

        resolved_events = [event for event in events if event.event_type == "thread_resolved"]
        reopened_events = [event for event in events if event.event_type == "thread_reopened"]

        self.assertEqual(1, len(resolved_events))
        self.assertEqual("2 review threads were resolved on PR #1049.", resolved_events[0].summary)
        self.assertEqual(["thread-1", "thread-2"], resolved_events[0].payload["thread_ids"])
        self.assertEqual("thread-2", resolved_events[0].payload["thread_id"])
        self.assertEqual("thread_resolved:bang9", resolved_events[0].payload["condition_key"])
        self.assertEqual(1, len(reopened_events))
        self.assertEqual("A review thread was reopened on PR #1049.", reopened_events[0].summary)

    def test_review_requested_dedupe_is_stable_across_unrelated_pr_updates(self):
        base = {
            "owner": "sendbird",
            "repo": "ai-agent-js",
            "number": 1049,
            "url": PR_URL,
            "title": "Improve the tool runner",
            "author": {"login": "bang9"},
            "reviewRequests": [{"login": "irene"}],
            "updatedAt": "2026-05-11T10:00:00Z",
        }
        noisy_update = dict(base)
        noisy_update["updatedAt"] = "2026-05-11T11:00:00Z"

        first = [event for event in classify_pr(base, current_user="irene") if event.event_type == "review_requested"]
        second = [
            event
            for event in classify_pr(noisy_update, current_user="irene")
            if event.event_type == "review_requested"
        ]

        self.assertEqual(1, len(first))
        self.assertEqual(1, len(second))
        self.assertEqual(first[0].dedupe_key, second[0].dedupe_key)
        self.assertEqual("review_requested:irene", first[0].payload["condition_key"])

    def test_render_notification_keeps_desktop_text_compact(self):
        from pr_watch.notifications import render_notification

        with tempfile.TemporaryDirectory() as tmpdir:
            event = route_event(make_store(tmpdir), make_review_event(), sessions=[])
            title, message = render_notification(event)

        self.assertEqual("ai-agent-js #1049 needs attention", title)
        self.assertEqual("bang9 pushed new commits to PR #1049", message)
        self.assertNotIn("https://", title + message)
        self.assertNotIn("sendbird/ai-agent-js:", message)

    def test_desktop_notification_omits_pr_url_from_macos_script(self):
        from pr_watch.notifications import DesktopNotifier

        with tempfile.TemporaryDirectory() as tmpdir:
            event = route_event(make_store(tmpdir), make_review_event(), sessions=[])

            with patch("pr_watch.notifications.platform.system", return_value="Darwin"):
                with patch("pr_watch.notifications.subprocess.run") as run:
                    run.return_value.returncode = 0
                    run.return_value.stderr = ""
                    run.return_value.stdout = ""

                    result = DesktopNotifier().send(
                        "ai-agent-js #1049 needs attention",
                        "bang9 pushed new commits to PR #1049",
                        event,
                    )

        script = run.call_args.args[0][2]
        self.assertTrue(result.ok)
        self.assertNotIn(event.pr_url, script)
        self.assertNotIn("subtitle", script)

    def test_desktop_notification_uses_terminal_notifier_activation_when_host_is_known(self):
        from pr_watch.notifications import RecordingNotifier, notify_event

        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                host="conductor",
            )
            inbox_item = route_event(store, make_review_event("activation"), sessions=[])
            notifier = RecordingNotifier()

            result = notify_event(store, inbox_item.event_id, mode="desktop", notifier=notifier)

            self.assertEqual("notified", result.action)
            self.assertEqual("com.conductor.app", notifier.messages[0]["activation_bundle_id"])

    def test_desktop_notifier_activates_host_with_terminal_notifier_when_available(self):
        from pr_watch.notifications import DesktopNotifier

        with tempfile.TemporaryDirectory() as tmpdir:
            event = route_event(make_store(tmpdir), make_review_event(), sessions=[])

            with patch("pr_watch.notifications.platform.system", return_value="Darwin"):
                with patch("pr_watch.notifications.shutil.which", return_value="/opt/homebrew/bin/terminal-notifier"):
                    with patch("pr_watch.notifications.subprocess.run") as run:
                        run.return_value.returncode = 0
                        run.return_value.stderr = ""
                        run.return_value.stdout = ""

                        result = DesktopNotifier().send(
                            "ai-agent-js #1049 needs attention",
                            "bang9 pushed new commits to PR #1049",
                            event,
                            activation_bundle_id="com.conductor.app",
                        )

        command = run.call_args.args[0]
        self.assertTrue(result.ok)
        self.assertEqual("/opt/homebrew/bin/terminal-notifier", command[0])
        self.assertIn("-activate", command)
        self.assertEqual("com.conductor.app", command[command.index("-activate") + 1])
        self.assertNotIn(event.pr_url, command)

    def test_poll_once_skips_draft_prs_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = Path(tmpdir) / "prs.json"
            fixture.write_text(
                """
                [
                  {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": "https://github.com/sendbird/ai-agent-js/pull/1049",
                    "isDraft": true,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                      {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z"
                      }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z"
                  }
                ]
                """,
                encoding="utf-8",
            )

            items = poll_once(make_store(tmpdir), "irene", fixture=str(fixture), sessions=[])

            self.assertEqual([], items)

    def test_poll_once_can_include_draft_prs_when_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = Path(tmpdir) / "prs.json"
            fixture.write_text(
                """
                [
                  {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": "https://github.com/sendbird/ai-agent-js/pull/1049",
                    "isDraft": true,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                      {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z"
                      }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z"
                  }
                ]
                """,
                encoding="utf-8",
            )

            items = poll_once(
                make_store(tmpdir),
                "irene",
                fixture=str(fixture),
                sessions=[],
                include_drafts=True,
            )

            self.assertEqual(1, len(items))
            self.assertEqual("author_push_after_review", items[0].event_type)

    def test_mcp_handlers_share_watcher_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.mcp_server import bind_pr, list_inbox

            bind_result = bind_pr(
                pr=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                state_dir=tmpdir,
            )
            inbox_result = list_inbox(state_dir=tmpdir)

            self.assertEqual("codex-abc", bind_result["binding"]["session_id"])
            self.assertEqual([], inbox_result["events"])

    def test_mcp_notify_and_list_notifications_share_watcher_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.mcp_server import list_notifications, notify

            store = make_store(tmpdir)
            create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
            )
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            inbox_item = route_event(store, event, sessions=[])

            notify_result = notify(inbox_item.event_id, mode="in_app", state_dir=tmpdir)
            notifications_result = list_notifications(state_dir=tmpdir)

            self.assertEqual("notified", notify_result["action"])
            self.assertEqual(["in_app"], notify_result["channels"])
            self.assertEqual(1, len(notifications_result["notifications"]))
            self.assertEqual("in_app", notifications_result["notifications"][0]["channel"])

    def test_mcp_notification_ack_marks_in_app_item_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.mcp_server import ack_notification, list_notifications, notify

            store = make_store(tmpdir)
            create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
            )
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            inbox_item = route_event(store, event, sessions=[])
            notify(inbox_item.event_id, mode="in_app", state_dir=tmpdir)
            notification_id = list_notifications(state_dir=tmpdir)["notifications"][0]["notification_id"]

            ack_result = ack_notification(notification_id=notification_id, state_dir=tmpdir)

            self.assertEqual("acked", ack_result["action"])
            self.assertEqual([], list_notifications(state_dir=tmpdir)["notifications"])
            self.assertEqual(1, len(list_notifications(state_dir=tmpdir, include_done=True)["notifications"]))

    def test_author_feedback_events_are_actionable_and_deduped(self):
        pr = {
            "owner": "sendbird",
            "repo": "ai-agent-js",
            "number": 1049,
            "url": PR_URL,
            "title": "Improve the tool runner",
            "author": {"login": "irene"},
            "reviewDecision": "CHANGES_REQUESTED",
            "statusCheckRollup": [
                {"name": "lint", "conclusion": "SUCCESS"},
                {"name": "test", "conclusion": "FAILURE"},
            ],
            "mergeStateStatus": "CLEAN",
            "latestReviews": [
                {
                    "author": {"login": "bang9"},
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-05-11T10:00:00Z",
                }
            ],
            "updatedAt": "2026-05-11T10:03:00Z",
        }

        first = classify_pr(pr, current_user="irene")
        second = classify_pr(pr, current_user="irene")

        event_types = {event.event_type for event in first}
        self.assertIn("requested_changes", event_types)
        self.assertIn("ci_failed", event_types)
        self.assertTrue(all(event.actionable for event in first))
        self.assertEqual(
            [event.dedupe_key for event in first],
            [event.dedupe_key for event in second],
        )

    def test_ci_failed_dedupe_ignores_unrelated_pr_updated_at_changes(self):
        base = {
            "owner": "sendbird",
            "repo": "ai-agent-js",
            "number": 1049,
            "url": PR_URL,
            "title": "Improve the tool runner",
            "author": {"login": "irene"},
            "headRefOid": "head-sha-1",
            "statusCheckRollup": [
                {"name": "sync-approved-label", "conclusion": "FAILURE"},
                {"name": "sync-approved-label", "conclusion": "FAILURE"},
            ],
            "updatedAt": "2026-05-15T03:03:00Z",
        }
        noisy_update = {**base, "updatedAt": "2026-05-15T04:22:00Z"}

        first = [event for event in classify_pr(base, current_user="irene") if event.event_type == "ci_failed"]
        second = [event for event in classify_pr(noisy_update, current_user="irene") if event.event_type == "ci_failed"]

        self.assertEqual(1, len(first))
        self.assertEqual(1, len(second))
        self.assertEqual(first[0].dedupe_key, second[0].dedupe_key)
        self.assertEqual("CI failed on PR #1049: sync-approved-label.", first[0].summary)
        self.assertEqual(
            {
                "failed_checks": ["sync-approved-label"],
                "headRefOid": "head-sha-1",
                "condition_key": "head-sha-1:sync-approved-label",
            },
            first[0].payload,
        )

    def test_requested_changes_dedupe_is_stable_without_review_object(self):
        base = {
            "owner": "sendbird",
            "repo": "ai-agent-js",
            "number": 1049,
            "url": PR_URL,
            "author": {"login": "irene"},
            "reviewDecision": "CHANGES_REQUESTED",
            "updatedAt": "2026-05-15T03:03:00Z",
        }
        noisy_update = {**base, "updatedAt": "2026-05-15T04:22:00Z"}

        first = [event for event in classify_pr(base, current_user="irene") if event.event_type == "requested_changes"]
        second = [
            event
            for event in classify_pr(noisy_update, current_user="irene")
            if event.event_type == "requested_changes"
        ]

        self.assertEqual(1, len(first))
        self.assertEqual(1, len(second))
        self.assertEqual(first[0].dedupe_key, second[0].dedupe_key)
        self.assertEqual("requested_changes:CHANGES_REQUESTED:reviewer", first[0].payload["condition_key"])

    def test_legacy_ci_failed_event_is_reused_after_condition_dedupe_upgrade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            old_event = ClassifiedEvent(
                pr=PullRequestRef(
                    owner="sendbird",
                    repo="ai-agent-js",
                    number=1049,
                    url=PR_URL,
                    title="Improve the tool runner",
                    head_ref="review/pr-1049",
                ),
                role="author",
                event_type="ci_failed",
                summary="CI failed on PR #1049: sync-approved-label.",
                actor="github",
                actionable=True,
                dedupe_key=stable_id(
                    "sendbird/ai-agent-js",
                    1049,
                    "author",
                    "ci_failed",
                    "github",
                    "2026-05-15T03:03:00Z",
                ),
                payload={"failed_checks": ["sync-approved-label"]},
            )
            old_item = store.upsert_event(
                old_event,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=None,
                confidence="high",
                evidence=["legacy event"],
            )

            upgraded_event = [
                event
                for event in classify_pr(
                    {
                        "owner": "sendbird",
                        "repo": "ai-agent-js",
                        "number": 1049,
                        "url": PR_URL,
                        "author": {"login": "irene"},
                        "headRefOid": "head-sha-1",
                        "statusCheckRollup": [{"name": "sync-approved-label", "conclusion": "FAILURE"}],
                        "updatedAt": "2026-05-15T04:22:00Z",
                    },
                    current_user="irene",
                )
                if event.event_type == "ci_failed"
            ][0]

            routed = route_event(store, upgraded_event, sessions=[])

            self.assertEqual(old_item.event_id, routed.event_id)
            self.assertEqual(old_item.dedupe_key, routed.dedupe_key)
            self.assertEqual("needs_confirmation", routed.status)

    def test_author_inline_review_comments_are_actionable_and_deduped(self):
        pr = {
            "owner": "sendbird",
            "repo": "ai-agent-js",
            "number": 1061,
            "url": "https://github.com/sendbird/ai-agent-js/pull/1061",
            "title": "Update agent runtime",
            "author": {"login": "irene"},
            "reviewComments": [
                {
                    "id": 2313318077,
                    "author": {"login": "bang9"},
                    "body": "Can we simplify this branch?",
                    "path": "src/runtime.ts",
                    "updatedAt": "2026-05-15T02:42:33Z",
                }
            ],
            "updatedAt": "2026-05-15T03:08:39Z",
        }

        first = classify_pr(pr, current_user="irene")
        second = classify_pr(pr, current_user="irene")

        review_comment_events = [event for event in first if event.event_type == "human_review_comment"]

        self.assertEqual(1, len(review_comment_events))
        self.assertEqual("author", review_comment_events[0].role)
        self.assertEqual("bang9", review_comment_events[0].actor)
        self.assertIn("inline review comment", review_comment_events[0].summary)
        self.assertEqual(2313318077, review_comment_events[0].payload["comment_id"])
        self.assertEqual(
            [event.dedupe_key for event in first],
            [event.dedupe_key for event in second],
        )

    def test_author_inline_review_comments_are_coalesced_per_actor(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1061,
                "url": "https://github.com/sendbird/ai-agent-js/pull/1061",
                "title": "Update agent runtime",
                "author": {"login": "irene"},
                "reviewComments": [
                    {
                        "id": 2313318077,
                        "author": {"login": "bang9"},
                        "body": "Can we simplify this branch?",
                        "path": "src/runtime.ts",
                        "updatedAt": "2026-05-15T02:42:33Z",
                    },
                    {
                        "id": 2313318078,
                        "author": {"login": "bang9"},
                        "body": "This needs a guard.",
                        "path": "src/runner.ts",
                        "updatedAt": "2026-05-15T02:43:33Z",
                    },
                    {
                        "id": 2313318079,
                        "author": {"login": "bang9"},
                        "body": "Can we add a regression test?",
                        "path": "tests/runtime.test.ts",
                        "updatedAt": "2026-05-15T02:44:33Z",
                    },
                ],
                "updatedAt": "2026-05-15T03:08:39Z",
            },
            current_user="irene",
        )

        review_comment_events = [event for event in events if event.event_type == "human_review_comment"]

        self.assertEqual(1, len(review_comment_events))
        self.assertEqual("bang9 left 3 inline review comments on your PR #1061.", review_comment_events[0].summary)
        self.assertEqual([2313318077, 2313318078, 2313318079], review_comment_events[0].payload["comment_ids"])
        self.assertEqual(2313318079, review_comment_events[0].payload["comment_id"])
        self.assertEqual("human_review_comment:bang9", review_comment_events[0].payload["condition_key"])

    def test_author_service_inline_review_comments_are_muted(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1061,
                "url": "https://github.com/sendbird/ai-agent-js/pull/1061",
                "author": {"login": "irene"},
                "reviewComments": [
                    {
                        "id": "actions-inline-comment",
                        "author": {"login": "github-actions", "type": "Bot"},
                        "body": "Workflow run completed.",
                        "updatedAt": "2026-05-15T02:42:33Z",
                    },
                    {
                        "id": "dependabot-inline-comment",
                        "author": {"login": "dependabot[bot]", "type": "Bot"},
                        "body": "Bumps a dependency.",
                        "updatedAt": "2026-05-15T03:08:39Z",
                    },
                    {
                        "id": "human-inline-comment",
                        "author": {"login": "bang9", "type": "User"},
                        "body": "Can we simplify this branch?",
                        "updatedAt": "2026-05-15T03:09:39Z",
                    },
                ],
                "updatedAt": "2026-05-15T03:09:39Z",
            },
            current_user="irene",
        )

        review_comment_events = [event for event in events if event.event_type == "human_review_comment"]

        self.assertEqual(["bang9"], [event.actor for event in review_comment_events])

    def test_author_service_deploy_preview_comments_are_muted(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1058,
                "url": "https://github.com/sendbird/ai-agent-js/pull/1058",
                "author": {"login": "irene"},
                "comments": [
                    {
                        "id": "netlify-comment",
                        "author": {"login": "netlify"},
                        "body": "Deploy Preview for ai-agent-js is ready!",
                        "authorAssociation": "NONE",
                        "updatedAt": "2026-05-14T01:41:15Z",
                    },
                    {
                        "id": "vercel-comment",
                        "author": {"login": "vercel"},
                        "body": "Preview Deployment is ready.",
                        "authorAssociation": "NONE",
                        "updatedAt": "2026-05-14T01:42:15Z",
                    },
                    {
                        "id": "actions-comment",
                        "author": {"login": "github-actions"},
                        "body": "Workflow run completed.",
                        "updatedAt": "2026-05-14T01:43:15Z",
                    },
                    {
                        "id": "dependabot-comment",
                        "author": {"login": "dependabot[bot]", "type": "Bot"},
                        "body": "Bumps a dependency.",
                        "updatedAt": "2026-05-14T01:44:15Z",
                    },
                    {
                        "id": "renovate-comment",
                        "author": {"login": "renovate[bot]", "type": "Bot"},
                        "body": "Renovate update.",
                        "updatedAt": "2026-05-14T01:45:15Z",
                    },
                    {
                        "id": "human-comment",
                        "author": {"login": "bang9"},
                        "body": "Can you look at the failing test?",
                        "authorAssociation": "MEMBER",
                        "updatedAt": "2026-05-14T01:46:15Z",
                    },
                ],
                "updatedAt": "2026-05-14T01:46:15Z",
            },
            current_user="irene",
        )

        comment_events = [event for event in events if event.event_type == "human_comment"]

        self.assertEqual(["bang9"], [event.actor for event in comment_events])

    def test_author_pr_comments_are_coalesced_per_actor(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1058,
                "url": "https://github.com/sendbird/ai-agent-js/pull/1058",
                "author": {"login": "irene"},
                "comments": [
                    {
                        "id": "first-comment",
                        "author": {"login": "bang9"},
                        "body": "Can you look at the failing test?",
                        "authorAssociation": "MEMBER",
                        "updatedAt": "2026-05-14T01:46:15Z",
                    },
                    {
                        "id": "second-comment",
                        "author": {"login": "bang9"},
                        "body": "Also check the fixture.",
                        "authorAssociation": "MEMBER",
                        "updatedAt": "2026-05-14T01:47:15Z",
                    },
                ],
                "updatedAt": "2026-05-14T01:47:15Z",
            },
            current_user="irene",
        )

        comment_events = [event for event in events if event.event_type == "human_comment"]

        self.assertEqual(1, len(comment_events))
        self.assertEqual("bang9 left 2 comments on your PR #1058.", comment_events[0].summary)
        self.assertEqual(["first-comment", "second-comment"], comment_events[0].payload["comment_ids"])
        self.assertEqual("second-comment", comment_events[0].payload["comment_id"])
        self.assertEqual("human_comment:bang9", comment_events[0].payload["condition_key"])

    def test_blocked_merge_state_with_mergeable_pr_is_not_a_merge_conflict(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1058,
                "url": "https://github.com/sendbird/ai-agent-js/pull/1058",
                "author": {"login": "irene"},
                "mergeStateStatus": "BLOCKED",
                "mergeable": "MERGEABLE",
                "updatedAt": "2026-05-12T10:00:00Z",
            },
            current_user="irene",
        )

        self.assertNotIn("merge_conflict", {event.event_type for event in events})

    def test_dirty_merge_state_emits_merge_conflict(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1058,
                "url": "https://github.com/sendbird/ai-agent-js/pull/1058",
                "author": {"login": "irene"},
                "mergeStateStatus": "DIRTY",
                "mergeable": "MERGEABLE",
                "updatedAt": "2026-05-12T10:00:00Z",
            },
            current_user="irene",
        )

        self.assertIn("merge_conflict", {event.event_type for event in events})

    def test_conflicting_mergeable_state_emits_merge_conflict(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1058,
                "url": "https://github.com/sendbird/ai-agent-js/pull/1058",
                "author": {"login": "irene"},
                "mergeStateStatus": "UNKNOWN",
                "mergeable": "CONFLICTING",
                "updatedAt": "2026-05-12T10:00:00Z",
            },
            current_user="irene",
        )

        self.assertIn("merge_conflict", {event.event_type for event in events})

    def test_linked_github_issue_comment_becomes_actionable_pr_event(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "url": PR_URL,
                "author": {"login": "bang9"},
                "latestReviews": [
                    {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z",
                    }
                ],
                "linkedIssues": [
                    {
                        "number": 321,
                        "url": "https://github.com/sendbird/ai-agent-js/issues/321",
                        "title": "Track review follow-up",
                        "comments": [
                            {
                                "id": 77,
                                "author": {"login": "bang9"},
                                "body": "Can we also handle Jira links?",
                                "updatedAt": "2026-05-11T11:00:00Z",
                            }
                        ],
                    }
                ],
                "updatedAt": "2026-05-11T11:00:00Z",
            },
            current_user="irene",
        )

        linked_events = [event for event in events if event.event_type == "linked_issue_comment"]

        self.assertEqual(1, len(linked_events))
        self.assertEqual("reviewer", linked_events[0].role)
        self.assertEqual("bang9", linked_events[0].actor)
        self.assertIn("issue #321", linked_events[0].summary)
        self.assertEqual("github_issue", linked_events[0].payload["source"])

    def test_linked_issue_comments_are_coalesced_per_issue_actor(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "url": PR_URL,
                "author": {"login": "bang9"},
                "latestReviews": [
                    {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z",
                    }
                ],
                "linkedIssues": [
                    {
                        "number": 321,
                        "url": "https://github.com/sendbird/ai-agent-js/issues/321",
                        "title": "Track review follow-up",
                        "comments": [
                            {
                                "id": 77,
                                "author": {"login": "bang9"},
                                "body": "Can we also handle Jira links?",
                                "updatedAt": "2026-05-11T11:00:00Z",
                            },
                            {
                                "id": 78,
                                "author": {"login": "bang9"},
                                "body": "And can we wire the Slack link?",
                                "updatedAt": "2026-05-11T11:05:00Z",
                            },
                            {
                                "id": 79,
                                "author": {"login": "bang9"},
                                "body": "One more follow-up.",
                                "updatedAt": "2026-05-11T11:10:00Z",
                            },
                        ],
                    }
                ],
                "updatedAt": "2026-05-11T11:10:00Z",
            },
            current_user="irene",
        )

        linked_events = [event for event in events if event.event_type == "linked_issue_comment"]

        self.assertEqual(1, len(linked_events))
        self.assertEqual("bang9 left 3 comments on linked issue #321 for PR #1049.", linked_events[0].summary)
        self.assertEqual([77, 78, 79], linked_events[0].payload["comment_ids"])
        self.assertEqual(79, linked_events[0].payload["comment_id"])
        self.assertEqual("linked_issue_comment:321:bang9", linked_events[0].payload["condition_key"])

    def test_duplicate_linked_issue_sources_emit_one_comment_event(self):
        linked_issue = {
            "number": 321,
            "url": "https://github.com/sendbird/ai-agent-js/issues/321",
            "title": "Track review follow-up",
            "comments": [
                {
                    "id": 77,
                    "author": {"login": "bang9"},
                    "body": "Can we also handle Jira links?",
                    "updatedAt": "2026-05-11T11:00:00Z",
                }
            ],
        }
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "url": PR_URL,
                "author": {"login": "bang9"},
                "latestReviews": [
                    {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z",
                    }
                ],
                "linkedIssues": [linked_issue],
                "closingIssuesReferences": [linked_issue],
                "updatedAt": "2026-05-11T11:00:00Z",
            },
            current_user="irene",
        )

        linked_events = [event for event in events if event.event_type == "linked_issue_comment"]

        self.assertEqual(1, len(linked_events))
        self.assertEqual([77], linked_events[0].payload["comment_ids"])

    def test_own_linked_issue_comments_are_muted(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "url": PR_URL,
                "author": {"login": "bang9"},
                "latestReviews": [
                    {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z",
                    }
                ],
                "linkedIssues": [
                    {
                        "number": 321,
                        "url": "https://github.com/sendbird/ai-agent-js/issues/321",
                        "comments": [
                            {
                                "id": 78,
                                "author": {"login": "irene"},
                                "body": "My own note",
                                "updatedAt": "2026-05-11T11:00:00Z",
                            }
                        ],
                    }
                ],
                "updatedAt": "2026-05-11T11:00:00Z",
            },
            current_user="irene",
        )

        self.assertNotIn("linked_issue_comment", {event.event_type for event in events})

    def test_linked_issue_service_comments_are_muted(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "url": PR_URL,
                "author": {"login": "bang9"},
                "latestReviews": [
                    {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z",
                    }
                ],
                "linkedIssues": [
                    {
                        "number": 321,
                        "url": "https://github.com/sendbird/ai-agent-js/issues/321",
                        "comments": [
                            {
                                "id": 79,
                                "author": {"login": "netlify"},
                                "body": "Deploy Preview for linked issue is ready!",
                                "authorAssociation": "NONE",
                                "updatedAt": "2026-05-11T11:00:00Z",
                            },
                            {
                                "id": 80,
                                "author": {"login": "bang9"},
                                "body": "This needs a real follow-up.",
                                "authorAssociation": "MEMBER",
                                "updatedAt": "2026-05-11T11:01:00Z",
                            },
                        ],
                    }
                ],
                "updatedAt": "2026-05-11T11:01:00Z",
            },
            current_user="irene",
        )

        linked_events = [event for event in events if event.event_type == "linked_issue_comment"]

        self.assertEqual(["bang9"], [event.actor for event in linked_events])

    def test_explicit_bind_creates_confirmed_binding_used_as_high_confidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            binding = create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
            )

            self.assertTrue(binding.confirmed)
            self.assertEqual("explicit_bind", binding.confirmation_source)

            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]

            inbox_item = route_event(store, event, sessions=[])

            self.assertEqual("high", inbox_item.confidence)
            self.assertEqual(binding.binding_id, inbox_item.binding_id)
            self.assertIn("confirmed binding", " ".join(inbox_item.evidence))

    def test_existing_state_db_migrates_bindings_active_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.sqlite"
            with sqlite3.connect(path) as conn:
                conn.executescript(
                    """
                    create table bindings (
                      binding_id text primary key,
                      repo_owner text not null,
                      repo_name text not null,
                      pr_number integer not null,
                      pr_url text not null,
                      role text not null,
                      agent text not null,
                      session_id text not null,
                      cwd text not null default '',
                      branch text not null default '',
                      host text,
                      confidence text not null,
                      confirmed integer not null,
                      confirmation_source text not null,
                      evidence_json text not null,
                      created_at text not null,
                      updated_at text not null,
                      last_event_at text not null default ''
                    );
                    """
                )

            StateStore(path)

            with sqlite3.connect(path) as conn:
                columns = {row[1] for row in conn.execute("pragma table_info(bindings)").fetchall()}
            self.assertIn("active", columns)

    def test_find_confirmed_binding_ignores_superseded_inactive_bindings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            old_binding = create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-old",
            )
            new_candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-new",
                confirmed=False,
                confirmation_source="rebind_candidate",
                evidence=["new candidate"],
            )

            confirmed = store.confirm_binding(new_candidate.binding_id)

            self.assertTrue(confirmed.active)
            self.assertFalse(store.get_binding(old_binding.binding_id).active)
            self.assertEqual(confirmed.binding_id, store.find_confirmed_binding(make_review_event()).binding_id)

    def test_explicit_bind_supersedes_previous_active_binding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            old_binding = create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-old",
            )
            new_binding = create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-new",
            )

            self.assertFalse(store.get_binding(old_binding.binding_id).active)
            self.assertTrue(store.get_binding(new_binding.binding_id).active)
            self.assertEqual(new_binding.binding_id, store.find_confirmed_binding(make_review_event()).binding_id)

    def test_confirm_binding_for_event_confirms_without_queueing_or_resuming(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.mcp_server import confirm_binding_for_event

            store = make_store(tmpdir)
            event = make_review_event()
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-candidate",
                confirmed=False,
                confirmation_source="inferred_candidate",
                evidence=["session text contains exact PR URL"],
            )
            inbox_item = store.upsert_event(
                event,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=candidate.binding_id,
                confidence="high",
                evidence=["first inferred binding requires approval"],
            )

            result = confirm_binding_for_event(
                event_id=inbox_item.event_id,
                mirror_now=False,
                state_dir=tmpdir,
            )

            stored = store.get_event(inbox_item.event_id)
            self.assertEqual("confirmed_binding", result["action"])
            self.assertTrue(store.get_binding(candidate.binding_id).confirmed)
            self.assertEqual("pending", stored.status)
            self.assertEqual("awaiting_approval", stored.delivery_status)
            self.assertEqual("high", stored.confidence)
            self.assertEqual([], store.list_queue())

    def test_confirm_binding_for_event_promotes_sibling_confirmation_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.workflow import confirm_binding_for_event

            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-candidate",
                confirmed=False,
                confirmation_source="inferred_candidate",
                evidence=["session text contains exact PR URL"],
            )
            first = store.upsert_event(
                make_review_event("review-requested"),
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=candidate.binding_id,
                confidence="high",
                evidence=["first inferred binding requires approval"],
            )
            second = store.upsert_event(
                make_review_event("linked-issue-comment"),
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=candidate.binding_id,
                confidence="high",
                evidence=["first inferred binding requires approval"],
            )

            result = confirm_binding_for_event(store, first.event_id, mirror_now=False)

            stored_first = store.get_event(first.event_id)
            stored_second = store.get_event(second.event_id)
            self.assertEqual("confirmed_binding", result["action"])
            self.assertTrue(store.get_binding(candidate.binding_id).confirmed)
            self.assertEqual("pending", stored_first.status)
            self.assertEqual("awaiting_approval", stored_first.delivery_status)
            self.assertEqual("pending", stored_second.status)
            self.assertEqual("awaiting_approval", stored_second.delivery_status)
            self.assertEqual(candidate.binding_id, stored_second.binding_id)

    def test_confirm_binding_and_mark_handled_confirms_then_dismisses_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.mcp_server import confirm_binding_and_mark_handled

            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="author",
                agent="claude",
                session_id="claude-candidate",
                confirmed=False,
                confirmation_source="inferred_candidate",
                evidence=["candidate"],
            )
            event = make_inbox_event(
                "sendbird",
                "ai-agent-js",
                1049,
                "handled",
                role="author",
                event_type="human_review_comment",
            )
            inbox_item = store.upsert_event(
                event,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=candidate.binding_id,
                confidence="high",
                evidence=["candidate"],
            )

            result = confirm_binding_and_mark_handled(
                event_id=inbox_item.event_id,
                state_dir=tmpdir,
            )

            stored = store.get_event(inbox_item.event_id)
            binding = store.get_binding(candidate.binding_id)
            self.assertEqual("confirmed_and_marked_handled", result["action"])
            self.assertTrue(binding.confirmed)
            self.assertTrue(binding.active)
            self.assertEqual("dismissed", stored.status)
            self.assertEqual("user_marked_handled", stored.delivery_status)
            self.assertIn("user confirmed binding and marked event handled", stored.evidence)
            self.assertEqual([], store.list_queue())

    def test_confirm_binding_and_mark_handled_promotes_sibling_confirmation_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.workflow import confirm_binding_and_mark_handled

            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-candidate",
                confirmed=False,
                confirmation_source="inferred_candidate",
                evidence=["session text contains exact PR URL"],
            )
            first = store.upsert_event(
                make_review_event("already-handled"),
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=candidate.binding_id,
                confidence="high",
                evidence=["first inferred binding requires approval"],
            )
            second = store.upsert_event(
                make_review_event("still-actionable"),
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=candidate.binding_id,
                confidence="high",
                evidence=["first inferred binding requires approval"],
            )

            result = confirm_binding_and_mark_handled(store, first.event_id)

            stored_first = store.get_event(first.event_id)
            stored_second = store.get_event(second.event_id)
            self.assertEqual("confirmed_and_marked_handled", result["action"])
            self.assertTrue(store.get_binding(candidate.binding_id).confirmed)
            self.assertEqual("dismissed", stored_first.status)
            self.assertEqual("user_marked_handled", stored_first.delivery_status)
            self.assertEqual("pending", stored_second.status)
            self.assertEqual("awaiting_approval", stored_second.delivery_status)
            self.assertEqual(candidate.binding_id, stored_second.binding_id)

    def test_cli_confirm_binding_can_select_candidate_without_triggering_delivery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-candidate",
                confirmed=False,
                confirmation_source="inferred_candidate",
                evidence=["candidate"],
            )
            inbox_item = store.upsert_event(
                make_review_event(),
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=candidate.binding_id,
                confidence="high",
                evidence=["candidate"],
            )
            output = StringIO()

            with redirect_stdout(output):
                code = main(["--state-dir", tmpdir, "confirm-binding", inbox_item.event_id, "--no-mirror"])

            self.assertEqual(0, code)
            self.assertIn("confirmed_binding", output.getvalue())
            self.assertTrue(store.get_binding(candidate.binding_id).confirmed)
            self.assertEqual([], store.list_queue())

    def test_cli_confirm_binding_can_mark_current_event_handled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-candidate",
                confirmed=False,
                confirmation_source="inferred_candidate",
                evidence=["candidate"],
            )
            inbox_item = store.upsert_event(
                make_review_event("handled-cli"),
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=candidate.binding_id,
                confidence="high",
                evidence=["candidate"],
            )
            output = StringIO()

            with redirect_stdout(output):
                code = main(
                    [
                        "--state-dir",
                        tmpdir,
                        "confirm-binding",
                        inbox_item.event_id,
                        "--mark-handled",
                        "--no-mirror",
                    ]
                )

            self.assertEqual(0, code)
            self.assertIn("confirmed_and_marked_handled", output.getvalue())
            self.assertTrue(store.get_binding(candidate.binding_id).confirmed)
            self.assertEqual("dismissed", store.get_event(inbox_item.event_id).status)
            self.assertEqual("user_marked_handled", store.get_event(inbox_item.event_id).delivery_status)
            self.assertEqual([], store.list_queue())

    def test_mcp_reject_binding_for_event_keeps_event_pending_without_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.mcp_server import reject_binding_for_event

            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-candidate",
                confirmed=False,
                active=False,
                confirmation_source="inferred_candidate",
                evidence=["candidate"],
            )
            inbox_item = store.upsert_event(
                make_review_event(),
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=candidate.binding_id,
                confidence="high",
                evidence=["candidate"],
            )

            result = reject_binding_for_event(event_id=inbox_item.event_id, state_dir=tmpdir)

            stored = store.get_event(inbox_item.event_id)
            rejected = store.get_binding(candidate.binding_id)
            self.assertEqual("rejected_binding", result["action"])
            self.assertFalse(rejected.active)
            self.assertFalse(rejected.confirmed)
            self.assertEqual("pending", stored.status)
            self.assertEqual("session_candidate_rejected", stored.delivery_status)
            self.assertEqual("", stored.binding_id)

    def test_cli_dismiss_event_marks_update_dismissed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            inbox_item = store.upsert_event(
                make_review_event(),
                status="pending",
                delivery_status="awaiting_approval",
                binding_id=None,
                confidence="high",
                evidence=["test"],
            )
            output = StringIO()

            with redirect_stdout(output):
                code = main(["--state-dir", tmpdir, "dismiss-event", inbox_item.event_id])

            stored = store.get_event(inbox_item.event_id)
            self.assertEqual(0, code)
            self.assertIn("dismissed_event", output.getvalue())
            self.assertEqual("dismissed", stored.status)
            self.assertEqual("user_dismissed", stored.delivery_status)

    def test_first_inferred_binding_requires_confirmation_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            session = SessionInfo(
                agent="claude",
                session_id="claude-123",
                title="Review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                last_activity_at="2026-05-11T09:30:00Z",
            )

            inbox_item = route_event(store, event, sessions=[session])
            binding = store.get_binding(inbox_item.binding_id)

            self.assertEqual("needs_confirmation", inbox_item.status)
            self.assertFalse(binding.confirmed)
            self.assertEqual("inferred_candidate", binding.confirmation_source)

            result = approve_event(
                store,
                inbox_item.event_id,
                session_state="unknown",
                runner=RecordingRunner(),
            )

            self.assertEqual("queued", result.action)
            self.assertTrue(store.get_binding(inbox_item.binding_id).confirmed)

    def test_first_inferred_binding_prefers_most_recent_matching_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            older_session = SessionInfo(
                agent="codex",
                session_id="codex-older",
                title="Review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                last_activity_at="2026-05-11T09:30:00Z",
            )
            newer_session = SessionInfo(
                agent="codex",
                session_id="codex-newer",
                title="Review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                last_activity_at="2026-05-11T10:30:00Z",
            )

            inbox_item = route_event(store, event, sessions=[older_session, newer_session])
            binding = store.get_binding(inbox_item.binding_id)

            self.assertEqual("needs_confirmation", inbox_item.status)
            self.assertEqual("codex-newer", binding.session_id)
            self.assertIn("preferred newest matching session", " ".join(inbox_item.evidence))

    def test_pr_review_session_beats_newer_pr_listing_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event = make_inbox_event("sendbird", "ai-agent-js", 1057, "author-push")
            listing_session = SessionInfo(
                agent="claude",
                session_id="show-pending-prs",
                title="Show pending PRs",
                cwd="/repo/ai-agent-js",
                branch="review-PRs",
                text=(
                    "gh search prs --repo=sendbird/ai-agent-js --state=open --review-requested=@me\n"
                    "| #1057 | Review needed | https://github.com/sendbird/ai-agent-js/pull/1057 |\n"
                    "| #1058 | Review needed | https://github.com/sendbird/ai-agent-js/pull/1058 |"
                ),
                last_activity_at="2026-05-15T06:09:00Z",
            )
            review_session = SessionInfo(
                agent="codex",
                session_id="review-1057",
                title="Review 1057",
                cwd="/repo/ai-agent-js",
                branch="review-pr-bang9",
                text=(
                    "Separate verification passed.\n"
                    "origin/pr/1057 packages/messenger-react/src/contexts/AgentProviderContainer.tsx\n"
                    "Posted inline comment: https://github.com/sendbird/ai-agent-js/pull/1057#discussion_r3245899037"
                ),
                last_activity_at="2026-05-15T04:26:29Z",
            )

            inbox_item = route_event(store, event, sessions=[listing_session, review_session])
            binding = store.get_binding(inbox_item.binding_id)

            self.assertEqual("needs_confirmation", inbox_item.status)
            self.assertEqual("review-1057", binding.session_id)
            self.assertIn("session title is focused on PR #1057", " ".join(inbox_item.evidence))

    def test_active_session_beats_newer_inactive_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            active_session = SessionInfo(
                agent="codex",
                session_id="codex-active",
                title="Review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                host="codex_app:active",
                last_activity_at="2026-05-11T09:30:00Z",
            )
            inactive_session = SessionInfo(
                agent="codex",
                session_id="codex-newer",
                title="Review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                last_activity_at="2026-05-11T10:30:00Z",
            )

            inbox_item = route_event(store, event, sessions=[inactive_session, active_session])
            binding = store.get_binding(inbox_item.binding_id)

            self.assertEqual("needs_confirmation", inbox_item.status)
            self.assertEqual("codex-active", binding.session_id)
            self.assertIn("active or focused session", " ".join(inbox_item.evidence))

    def test_equally_likely_sessions_stay_in_inbox_until_user_binds_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            first_session = SessionInfo(
                agent="codex",
                session_id="codex-first",
                title="Review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                last_activity_at="2026-05-11T10:30:00Z",
            )
            second_session = SessionInfo(
                agent="codex",
                session_id="codex-second",
                title="Review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                last_activity_at="2026-05-11T10:30:00Z",
            )

            inbox_item = route_event(store, event, sessions=[first_session, second_session])

            self.assertEqual("pending", inbox_item.status)
            self.assertEqual("ambiguous_session_candidates", inbox_item.delivery_status)
            self.assertIsNone(inbox_item.binding_id)
            self.assertIn("codex-first", " ".join(inbox_item.evidence))
            self.assertIn("codex-second", " ".join(inbox_item.evidence))

    def test_high_confidence_different_session_requires_rebind_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            active_binding = create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-old",
            )
            newer_session = SessionInfo(
                agent="codex",
                session_id="codex-new",
                title="Active review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                host="conductor:active",
                last_activity_at="2026-05-11T11:00:00Z",
            )

            inbox_item = route_event(store, make_review_event("rebind"), sessions=[newer_session])
            candidate = store.get_binding(inbox_item.binding_id)

            self.assertEqual("needs_confirmation", inbox_item.status)
            self.assertEqual("awaiting_rebind_confirmation", inbox_item.delivery_status)
            self.assertEqual("high", inbox_item.confidence)
            self.assertEqual("codex-new", candidate.session_id)
            self.assertFalse(candidate.confirmed)
            self.assertEqual("rebind_candidate", candidate.confirmation_source)
            self.assertEqual(active_binding.binding_id, store.find_confirmed_binding(make_review_event()).binding_id)

            rerouted = route_event(store, make_review_event("rebind"), sessions=[])

            self.assertEqual("needs_confirmation", rerouted.status)
            self.assertEqual("awaiting_rebind_confirmation", rerouted.delivery_status)
            self.assertEqual(candidate.binding_id, rerouted.binding_id)

    def test_confirmed_rebind_supersedes_previous_active_handler_for_future_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.workflow import confirm_binding_for_event

            store = make_store(tmpdir)
            old_binding = create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-old",
            )
            new_session = SessionInfo(
                agent="codex",
                session_id="codex-new",
                title="Active review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                host="conductor:active",
                last_activity_at="2026-05-11T11:00:00Z",
            )
            rebind_item = route_event(store, make_review_event("rebind-confirm"), sessions=[new_session])

            result = confirm_binding_for_event(store, rebind_item.event_id, mirror_now=False)
            next_item = route_event(store, make_review_event("future"), sessions=[])

            self.assertEqual("confirmed_binding", result["action"])
            self.assertFalse(store.get_binding(old_binding.binding_id).active)
            self.assertEqual("codex-new", store.find_confirmed_binding(make_review_event()).session_id)
            self.assertEqual(rebind_item.binding_id, next_item.binding_id)
            self.assertEqual("awaiting_approval", next_item.delivery_status)

    def test_ambiguous_rebind_candidates_do_not_use_old_active_binding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            active_binding = create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-old",
            )
            first = SessionInfo(
                agent="codex",
                session_id="codex-first",
                title="Review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                last_activity_at="2026-05-11T11:00:00Z",
            )
            second = SessionInfo(
                agent="codex",
                session_id="codex-second",
                title="Review PR 1049",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                text=f"Continuing review for {PR_URL}",
                last_activity_at="2026-05-11T11:00:00Z",
            )

            inbox_item = route_event(store, make_review_event("ambiguous-rebind"), sessions=[first, second])

            self.assertEqual("pending", inbox_item.status)
            self.assertEqual("ambiguous_session_candidates", inbox_item.delivery_status)
            self.assertIsNone(inbox_item.binding_id)
            self.assertEqual(active_binding.binding_id, store.find_confirmed_binding(make_review_event()).binding_id)

    def test_low_confidence_different_session_does_not_use_old_active_binding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            active_binding = create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-old",
            )
            weak_session = SessionInfo(
                agent="codex",
                session_id="codex-weak",
                title="General repo work",
                cwd="/repo/ai-agent-js",
                branch="main",
                text="Working somewhere in sendbird/ai-agent-js",
                last_activity_at="2026-05-11T11:00:00Z",
            )

            inbox_item = route_event(store, make_review_event("low-rebind"), sessions=[weak_session])

            self.assertEqual("pending", inbox_item.status)
            self.assertEqual("inbox_only", inbox_item.delivery_status)
            self.assertEqual("low", inbox_item.confidence)
            self.assertIsNone(inbox_item.binding_id)
            self.assertEqual(active_binding.binding_id, store.find_confirmed_binding(make_review_event()).binding_id)

    def test_desktop_notification_does_not_resume_or_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.notifications import RecordingNotifier, notify_event

            store = make_store(tmpdir)
            create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
            )
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            inbox_item = route_event(store, event, sessions=[])
            notifier = RecordingNotifier()

            result = notify_event(store, inbox_item.event_id, mode="desktop", notifier=notifier)
            stored = store.get_event(inbox_item.event_id)

            self.assertEqual("notified", result.action)
            self.assertEqual(["desktop"], result.channels)
            self.assertEqual(1, len(notifier.messages))
            self.assertEqual([], store.list_queue())
            self.assertEqual("pending", stored.status)
            self.assertEqual("awaiting_approval", stored.delivery_status)

    def test_in_app_notification_creates_pending_item_without_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.notifications import notify_event

            store = make_store(tmpdir)
            create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
            )
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            inbox_item = route_event(store, event, sessions=[])

            result = notify_event(store, inbox_item.event_id, mode="in_app")
            notifications = store.list_notifications()

            self.assertEqual("notified", result.action)
            self.assertEqual(1, len(notifications))
            self.assertEqual("in_app", notifications[0].channel)
            self.assertEqual("pending", notifications[0].status)
            self.assertEqual(PR_URL, notifications[0].target_url)
            self.assertEqual("pending", store.get_event(inbox_item.event_id).status)

    def test_polling_existing_queued_event_does_not_reset_delivery_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
            )
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            inbox_item = route_event(store, event, sessions=[])
            approve_event(store, inbox_item.event_id, session_state="unknown")

            rerouted = route_event(store, event, sessions=[])

            self.assertEqual("queued", rerouted.status)
            self.assertEqual("queued", rerouted.delivery_status)
            self.assertEqual("queued", store.get_event(inbox_item.event_id).status)

    def test_inbox_items_include_event_payload_for_mcp_clients(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "irene"},
                    "reviewDecision": "CHANGES_REQUESTED",
                    "latestReviews": [
                        {
                            "author": {"login": "bang9"},
                            "state": "CHANGES_REQUESTED",
                            "submittedAt": "2026-05-11T10:00:00Z",
                        }
                    ],
                    "updatedAt": "2026-05-11T10:03:00Z",
                },
                current_user="irene",
            )[0]
            inbox_item = route_event(store, event, sessions=[])

            self.assertEqual("CHANGES_REQUESTED", inbox_item.payload["reviewDecision"])

    def test_low_confidence_event_stays_in_inbox_without_waking_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            low_confidence_session = SessionInfo(
                agent="codex",
                session_id="codex-weak",
                title="General repo work",
                cwd="/repo/ai-agent-js",
                branch="main",
                text="Working somewhere in sendbird/ai-agent-js",
                last_activity_at="2026-05-10T00:00:00Z",
            )

            inbox_item = route_event(store, event, sessions=[low_confidence_session])

            self.assertEqual("pending", inbox_item.status)
            self.assertEqual("low", inbox_item.confidence)
            self.assertIsNone(inbox_item.binding_id)

    def test_unknown_session_state_queues_by_default_after_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
            )
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            inbox_item = route_event(store, event, sessions=[])
            runner = RecordingRunner()

            result = approve_event(
                store,
                inbox_item.event_id,
                session_state="unknown",
                runner=runner,
            )

            self.assertEqual("queued", result.action)
            self.assertEqual([], runner.commands)
            self.assertEqual(1, len(store.list_queue()))
            self.assertEqual("queued", store.get_event(inbox_item.event_id).status)

    def test_approving_already_queued_event_does_not_duplicate_queue_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
            )
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            inbox_item = route_event(store, event, sessions=[])

            first = approve_event(store, inbox_item.event_id, session_state="unknown", runner=RecordingRunner())
            second = approve_event(store, inbox_item.event_id, session_state="unknown", runner=RecordingRunner())

            self.assertEqual("queued", first.action)
            self.assertEqual("already_queued", second.action)
            self.assertEqual(1, len(store.list_queue()))

    def test_poll_once_fans_out_notifications_without_approving_delivery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.notifications import RecordingNotifier

            fixture = Path(tmpdir) / "prs.json"
            fixture.write_text(
                """
                [
                  {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": "https://github.com/sendbird/ai-agent-js/pull/1049",
                    "author": {"login": "bang9"},
                    "latestReviews": [
                      {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z"
                      }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z"
                  }
                ]
                """,
                encoding="utf-8",
            )
            store = make_store(tmpdir)
            create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
            )
            notifier = RecordingNotifier()

            items = poll_once(
                store,
                "irene",
                fixture=str(fixture),
                sessions=[],
                notification_mode="desktop",
                notifier=notifier,
            )

            self.assertEqual(1, len(items))
            self.assertEqual(1, len(notifier.messages))
            self.assertEqual([], store.list_queue())
            self.assertEqual("awaiting_approval", store.get_event(items[0].event_id).delivery_status)

    def test_poll_once_dismisses_stale_open_pr_events_for_polled_repo_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = Path(tmpdir) / "prs.json"
            fixture.write_text(
                """
                [
                  {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": "https://github.com/sendbird/ai-agent-js/pull/1049",
                    "author": {"login": "bang9"},
                    "updatedAt": "2026-05-12T10:00:00Z"
                  }
                ]
                """,
                encoding="utf-8",
            )
            store = make_store(tmpdir)
            stale_pending = store.upsert_event(
                make_inbox_event("sendbird", "ai-agent-js", 1055, "pending"),
                status="pending",
                delivery_status="awaiting_approval",
                binding_id=None,
                confidence="high",
                evidence=["pending stale event"],
            )
            stale_confirmation = store.upsert_event(
                make_inbox_event("sendbird", "ai-agent-js", 1056, "confirmation"),
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=None,
                confidence="high",
                evidence=["confirmation stale event"],
            )
            stale_inbox_only = store.upsert_event(
                make_inbox_event("sendbird", "ai-agent-js", 1057, "inbox-only"),
                status="pending",
                delivery_status="inbox_only",
                binding_id=None,
                confidence="low",
                evidence=["inbox only stale event"],
            )
            stale_queued = store.upsert_event(
                make_inbox_event("sendbird", "ai-agent-js", 1058, "queued"),
                status="queued",
                delivery_status="queued",
                binding_id=None,
                confidence="high",
                evidence=["queued stale event"],
            )
            store.enqueue(stale_queued.event_id, ["codex", "resume", "session", "prompt"], "prompt")
            done_same_repo = store.upsert_event(
                make_inbox_event("sendbird", "ai-agent-js", 1059, "done"),
                status="delivered",
                delivery_status="delivered",
                binding_id=None,
                confidence="high",
                evidence=["already delivered"],
            )
            unrelated_repo = store.upsert_event(
                make_inbox_event("other", "repo", 1055, "pending"),
                status="pending",
                delivery_status="awaiting_approval",
                binding_id=None,
                confidence="high",
                evidence=["unrelated repo"],
            )

            poll_once(store, "irene", repo="sendbird/ai-agent-js", fixture=str(fixture), sessions=[])

            for item in (stale_pending, stale_confirmation, stale_inbox_only, stale_queued):
                stored = store.get_event(item.event_id)
                self.assertEqual("dismissed", stored.status)
                self.assertEqual("stale_pr_not_open", stored.delivery_status)
                self.assertIn("no longer in the open PR list", stored.error)
            self.assertEqual([], store.list_queue())
            self.assertEqual("delivered", store.get_event(done_same_repo.event_id).status)
            self.assertEqual("delivered", store.get_event(done_same_repo.event_id).delivery_status)
            self.assertEqual("pending", store.get_event(unrelated_repo.event_id).status)
            self.assertEqual("awaiting_approval", store.get_event(unrelated_repo.event_id).delivery_status)

    def test_poll_once_dismisses_stale_open_pr_events_for_mixed_case_repo_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = Path(tmpdir) / "prs.json"
            fixture.write_text("[]", encoding="utf-8")
            store = make_store(tmpdir)
            stale_queued = store.upsert_event(
                make_inbox_event("AhyoungRyu", "claude-code", 5, "queued"),
                status="queued",
                delivery_status="queued",
                binding_id=None,
                confidence="high",
                evidence=["queued stale event"],
            )
            store.enqueue(stale_queued.event_id, ["codex", "resume", "session", "prompt"], "prompt")

            poll_once(store, "AhyoungRyu", repo="AhyoungRyu/claude-code", fixture=str(fixture), sessions=[])

            stored = store.get_event(stale_queued.event_id)
            self.assertEqual("dismissed", stored.status)
            self.assertEqual("stale_pr_not_open", stored.delivery_status)
            self.assertEqual([], store.list_queue())

    def test_poll_once_dismisses_open_pr_events_no_longer_classified_as_actionable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = Path(tmpdir) / "prs.json"
            fixture.write_text(
                """
                [
                  {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1058,
                    "url": "https://github.com/sendbird/ai-agent-js/pull/1058",
                    "author": {"login": "irene"},
                    "mergeStateStatus": "BLOCKED",
                    "mergeable": "MERGEABLE",
                    "updatedAt": "2026-05-12T10:00:00Z"
                  }
                ]
                """,
                encoding="utf-8",
            )
            store = make_store(tmpdir)
            stale_conflict = store.upsert_event(
                make_inbox_event(
                    "sendbird",
                    "ai-agent-js",
                    1058,
                    "stale-conflict",
                    role="author",
                    event_type="merge_conflict",
                ),
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=None,
                confidence="high",
                evidence=["old false conflict"],
            )
            store.enqueue(stale_conflict.event_id, ["codex", "resume", "session", "prompt"], "prompt")

            poll_once(store, "irene", repo="sendbird/ai-agent-js", fixture=str(fixture), sessions=[])

            stored = store.get_event(stale_conflict.event_id)
            self.assertEqual("dismissed", stored.status)
            self.assertEqual("stale_pr_event_not_current", stored.delivery_status)
            self.assertIn("no longer present in the current actionable PR state", stored.error)
            self.assertEqual([], store.list_queue())

    def test_poll_once_dismisses_stale_netlify_confirmation_when_comment_is_no_longer_actionable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = Path(tmpdir) / "prs.json"
            fixture.write_text(
                """
                [
                  {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1058,
                    "url": "https://github.com/sendbird/ai-agent-js/pull/1058",
                    "author": {"login": "irene"},
                    "comments": [
                      {
                        "id": "netlify-comment",
                        "author": {"login": "netlify"},
                        "body": "Deploy Preview for ai-agent-js is ready!",
                        "authorAssociation": "NONE",
                        "updatedAt": "2026-05-14T01:41:15Z"
                      }
                    ],
                    "updatedAt": "2026-05-14T01:41:15Z"
                  }
                ]
                """,
                encoding="utf-8",
            )
            store = make_store(tmpdir)
            stale_event = ClassifiedEvent(
                pr=PullRequestRef(
                    owner="sendbird",
                    repo="ai-agent-js",
                    number=1058,
                    url="https://github.com/sendbird/ai-agent-js/pull/1058",
                    title="PR 1058",
                    head_ref="pr-1058",
                ),
                role="author",
                event_type="human_comment",
                summary="netlify commented on your PR #1058.",
                actor="netlify",
                actionable=True,
                dedupe_key=stable_id(
                    "sendbird/ai-agent-js",
                    1058,
                    "author",
                    "human_comment",
                    "netlify",
                    "2026-05-14T01:41:15Z",
                ),
                payload={"comment_id": "netlify-comment"},
            )
            stale_confirmation = store.upsert_event(
                stale_event,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
                binding_id=None,
                confidence="high",
                evidence=["old netlify deploy preview comment"],
            )
            store.enqueue(stale_confirmation.event_id, ["codex", "resume", "session", "prompt"], "prompt")

            poll_once(store, "irene", repo="sendbird/ai-agent-js", fixture=str(fixture), sessions=[])

            stored = store.get_event(stale_confirmation.event_id)
            self.assertEqual("dismissed", stored.status)
            self.assertEqual("stale_pr_event_not_current", stored.delivery_status)
            self.assertIn("no longer present in the current actionable PR state", stored.error)
            self.assertEqual([], store.list_queue())

    def test_idle_resume_failure_preserves_inbox_item_with_recovery_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            create_explicit_binding(
                store,
                PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
            )
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "bang9"},
                    "latestReviews": [
                        {
                            "author": {"login": "irene"},
                            "state": "COMMENTED",
                            "submittedAt": "2026-05-11T09:00:00Z",
                        }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z",
                },
                current_user="irene",
            )[0]
            inbox_item = route_event(store, event, sessions=[])
            runner = RecordingRunner(CommandResult(2, "", "session not found"))

            result = approve_event(
                store,
                inbox_item.event_id,
                session_state="idle",
                runner=runner,
            )
            stored = store.get_event(inbox_item.event_id)

            self.assertEqual("failed", result.action)
            self.assertEqual("pending", stored.status)
            self.assertEqual("failed", stored.delivery_status)
            self.assertIn("codex exec resume codex-abc", stored.recovery_command)
            self.assertIn("session not found", stored.error)

    def test_cli_bind_poll_inbox_and_approve_queue_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = Path(tmpdir) / "prs.json"
            fixture.write_text(
                """
                [
                  {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": "https://github.com/sendbird/ai-agent-js/pull/1049",
                    "author": {"login": "bang9"},
                    "latestReviews": [
                      {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z"
                      }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z"
                  }
                ]
                """,
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                bind_code = main(
                    [
                        "--state-dir",
                        tmpdir,
                        "bind",
                        PR_URL,
                        "--role",
                        "reviewer",
                        "--agent",
                        "codex",
                        "--session-id",
                        "codex-abc",
                        "--cwd",
                        "/repo/ai-agent-js",
                        "--branch",
                        "review/pr-1049",
                    ]
                )
                poll_code = main(
                    [
                        "--state-dir",
                        tmpdir,
                        "daemon",
                        "--once",
                        "--fixture",
                        str(fixture),
                        "--user",
                        "irene",
                    ]
                )

            out = StringIO()
            with redirect_stdout(out):
                inbox_code = main(["--state-dir", tmpdir, "inbox"])

            self.assertEqual(0, bind_code)
            self.assertEqual(0, poll_code)
            self.assertEqual(0, inbox_code)
            self.assertIn("author_push_after_review", out.getvalue())

            store = make_store(tmpdir)
            event_id = store.list_events()[0].event_id
            with redirect_stdout(StringIO()):
                approve_code = main(
                    [
                        "--state-dir",
                        tmpdir,
                        "approve",
                        event_id,
                        "--session-state",
                        "unknown",
                    ]
                )

            self.assertEqual(0, approve_code)
            self.assertEqual("queued", store.get_event(event_id).status)
            self.assertEqual(1, len(store.list_queue()))

    def test_mcp_user_friendly_aliases_delegate_to_core_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from pr_watch.mcp_server import (
                approve_resume_session,
                bind_pr,
                check_pr_updates,
                queue_resume_session,
                show_in_app_notifications,
                show_pending_pr_actions,
            )

            bind_pr(
                pr=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-abc",
                cwd="/repo/ai-agent-js",
                branch="review/pr-1049",
                state_dir=tmpdir,
            )
            fixture = Path(tmpdir) / "prs.json"
            fixture.write_text(
                """
                [
                  {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": "https://github.com/sendbird/ai-agent-js/pull/1049",
                    "author": {"login": "bang9"},
                    "latestReviews": [
                      {
                        "author": {"login": "irene"},
                        "state": "COMMENTED",
                        "submittedAt": "2026-05-11T09:00:00Z"
                      }
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z"
                  }
                ]
                """,
                encoding="utf-8",
            )

            updates = check_pr_updates(
                fixture=str(fixture),
                user="irene",
                notification_mode="in_app",
                state_dir=tmpdir,
            )
            inbox = show_pending_pr_actions(state_dir=tmpdir)
            notifications = show_in_app_notifications(state_dir=tmpdir)
            event_id = inbox["events"][0]["event_id"]
            notify_only = approve_resume_session(
                event_id=event_id,
                session_state="working",
                busy_policy="notify_only",
                state_dir=tmpdir,
            )
            queued = queue_resume_session(event_id=event_id, state_dir=tmpdir)

            self.assertEqual(["author_push_after_review"], [event["event_type"] for event in updates["events"]])
            self.assertEqual(1, len(notifications["notifications"]))
            self.assertEqual("notify_only", notify_only["action"])
            self.assertEqual("queued", queued["action"])

    def test_cli_init_profiles_set_host_appropriate_notification_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with redirect_stdout(StringIO()):
                terminal_code = main(["--state-dir", tmpdir, "init", "--profile", "terminal"])
            self.assertEqual(0, terminal_code)
            self.assertEqual("desktop", load_config(tmpdir)["notification_mode"])

        with tempfile.TemporaryDirectory() as tmpdir:
            with redirect_stdout(StringIO()):
                conductor_code = main(["--state-dir", tmpdir, "init", "--profile", "conductor"])
            self.assertEqual(0, conductor_code)
            self.assertEqual("in_app", load_config(tmpdir)["notification_mode"])

        with tempfile.TemporaryDirectory() as tmpdir:
            with redirect_stdout(StringIO()):
                app_code = main(["--state-dir", tmpdir, "init", "--profile", "app"])
            self.assertEqual(0, app_code)
            self.assertEqual("in_app", load_config(tmpdir)["notification_mode"])

    def test_mcp_launch_config_uses_python_module_entrypoint(self):
        launch = build_mcp_launch_config(
            python_executable="/opt/pr-watch/bin/python",
            state_dir="/Users/irene.ryu/.pr-watch",
        )

        self.assertEqual("/opt/pr-watch/bin/python", launch.command)
        self.assertEqual(["-m", "pr_watch", "--state-dir", "/Users/irene.ryu/.pr-watch", "mcp"], launch.args)

    def test_codex_mcp_add_command_wraps_pr_watch_launch_config(self):
        launch = build_mcp_launch_config(python_executable="/opt/pr-watch/bin/python")

        command = build_codex_mcp_add_command("/Users/irene.ryu/bin/codex", launch)

        self.assertEqual(
            [
                "/Users/irene.ryu/bin/codex",
                "mcp",
                "add",
                "pr-watch",
                "--",
                "/opt/pr-watch/bin/python",
                "-m",
                "pr_watch",
                "mcp",
            ],
            command,
        )

    def test_install_mcp_hosts_registers_codex_and_conductor_binaries(self):
        runner = RecordingHostCommandRunner()
        results = install_mcp_hosts(
            target="all",
            python_executable="/opt/pr-watch/bin/python",
            state_dir="/Users/irene.ryu/.pr-watch",
            codex_binary="/Users/irene.ryu/bin/codex",
            conductor_codex_binary="/Applications/Conductor.app/Contents/Resources/bin/codex",
            runner=runner,
        )

        self.assertEqual(["codex-app", "conductor"], [result.host for result in results])
        self.assertEqual(["installed", "installed"], [result.status for result in results])
        self.assertEqual(
            [
                [
                    "/Users/irene.ryu/bin/codex",
                    "mcp",
                    "get",
                    "pr-watch",
                ],
                [
                    "/Users/irene.ryu/bin/codex",
                    "mcp",
                    "add",
                    "pr-watch",
                    "--",
                    "/opt/pr-watch/bin/python",
                    "-m",
                    "pr_watch",
                    "--state-dir",
                    "/Users/irene.ryu/.pr-watch",
                    "mcp",
                ],
                [
                    "/Applications/Conductor.app/Contents/Resources/bin/codex",
                    "mcp",
                    "get",
                    "pr-watch",
                ],
                [
                    "/Applications/Conductor.app/Contents/Resources/bin/codex",
                    "mcp",
                    "add",
                    "pr-watch",
                    "--",
                    "/opt/pr-watch/bin/python",
                    "-m",
                    "pr_watch",
                    "--state-dir",
                    "/Users/irene.ryu/.pr-watch",
                    "mcp",
                ],
            ],
            runner.commands,
        )

    def test_cli_install_mcp_dry_run_prints_codex_and_conductor_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = StringIO()

            with redirect_stdout(output):
                code = main(
                    [
                        "--state-dir",
                        tmpdir,
                        "install-mcp",
                        "--target",
                        "all",
                        "--python",
                        "/opt/pr-watch/bin/python",
                        "--codex-bin",
                        "/Users/irene.ryu/bin/codex",
                        "--conductor-codex-bin",
                        "/Applications/Conductor.app/Contents/Resources/bin/codex",
                        "--dry-run",
                    ]
                )

            self.assertEqual(0, code)
            text = output.getvalue()
            self.assertIn("codex-app", text)
            self.assertIn("conductor", text)
            self.assertIn("/opt/pr-watch/bin/python -m pr_watch --state-dir", text)

    def test_watch_repo_state_and_cli_manage_repositories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)

            self.assertEqual([], store.list_watch_repos())
            self.assertEqual("sendbird/ai-agent-js", store.add_watch_repo("Sendbird/AI-Agent-JS"))
            self.assertEqual(["sendbird/ai-agent-js"], store.list_watch_repos())
            self.assertEqual("sendbird/ai-agent-js", store.add_watch_repo("sendbird/ai-agent-js"))
            self.assertEqual(["sendbird/ai-agent-js"], store.list_watch_repos())

            output = StringIO()
            with redirect_stdout(output):
                add_code = main(["--state-dir", tmpdir, "watch", "add", "AhyoungRyu/claude-code"])
                list_code = main(["--state-dir", tmpdir, "watch", "list"])
                remove_code = main(["--state-dir", tmpdir, "watch", "remove", "sendbird/ai-agent-js"])
                clear_code = main(["--state-dir", tmpdir, "watch", "clear"])

            self.assertEqual(0, add_code)
            self.assertEqual(0, list_code)
            self.assertEqual(0, remove_code)
            self.assertEqual(0, clear_code)
            self.assertIn("watching ahyoungryu/claude-code", output.getvalue())
            self.assertIn("sendbird/ai-agent-js", output.getvalue())
            self.assertEqual([], store.list_watch_repos())

    def test_parse_github_remote_url_supports_common_forms(self):
        cases = {
            "https://github.com/Sendbird/AI-Agent-JS.git": "sendbird/ai-agent-js",
            "https://github.com/Sendbird/AI-Agent-JS": "sendbird/ai-agent-js",
            "git@github.com:Sendbird/AI-Agent-JS.git": "sendbird/ai-agent-js",
            "ssh://git@github.com/Sendbird/AI-Agent-JS.git": "sendbird/ai-agent-js",
        }

        for remote, expected in cases.items():
            with self.subTest(remote=remote):
                self.assertEqual(expected, parse_github_remote_url(remote))

        self.assertIsNone(parse_github_remote_url("https://example.com/sendbird/ai-agent-js.git"))

    def test_detect_current_repo_reads_git_remote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True, text=True, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "git@github.com:Sendbird/AI-Agent-JS.git"],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertEqual("sendbird/ai-agent-js", detect_current_repo(tmpdir))

    def test_setup_cli_adds_explicit_repo_without_service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = StringIO()
            with redirect_stdout(output):
                code = main(["--state-dir", tmpdir, "setup", "--repo", "Sendbird/AI-Agent-JS"])

            self.assertEqual(0, code)
            self.assertIn("watching sendbird/ai-agent-js", output.getvalue())
            self.assertIn("service install skipped", output.getvalue())
            self.assertEqual(["sendbird/ai-agent-js"], make_store(tmpdir).list_watch_repos())

    def test_setup_cli_current_repo_detects_remote(self):
        with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as repo_dir:
            subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, text=True, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/AhyoungRyu/claude-code.git"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            previous_cwd = os.getcwd()
            try:
                os.chdir(repo_dir)
                output = StringIO()
                with redirect_stdout(output):
                    code = main(["--state-dir", state_dir, "setup", "--current-repo"])
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(0, code)
            self.assertIn("watching ahyoungryu/claude-code", output.getvalue())
            self.assertEqual(["ahyoungryu/claude-code"], make_store(state_dir).list_watch_repos())

    def test_setup_cli_dry_run_does_not_write_watch_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = StringIO()
            with redirect_stdout(output):
                code = main(["--state-dir", tmpdir, "setup", "--repo", "sendbird/ai-agent-js", "--dry-run"])

            self.assertEqual(0, code)
            self.assertIn("would watch sendbird/ai-agent-js", output.getvalue())
            self.assertEqual([], make_store(tmpdir).list_watch_repos())

    def test_setup_cli_install_service_dry_run_prints_plist_without_writing_service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plist_path = Path(tmpdir) / "LaunchAgents" / "com.example.pr-watch.test.plist"
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--state-dir",
                        tmpdir,
                        "setup",
                        "--repo",
                        "sendbird/ai-agent-js",
                        "--install-service",
                        "--dry-run",
                        "--interval",
                        "90",
                        "--notification-mode",
                        "in_app",
                        "--label",
                        "com.example.pr-watch.test",
                        "--plist-path",
                        str(plist_path),
                    ]
                )

            text = output.getvalue()
            self.assertEqual(0, code)
            self.assertIn("would watch sendbird/ai-agent-js", text)
            self.assertIn("dry_run: com.example.pr-watch.test", text)
            self.assertIn("<key>StartInterval</key>", text)
            self.assertFalse(plist_path.exists())
            self.assertEqual([], make_store(tmpdir).list_watch_repos())

    def test_launchd_plist_runs_service_once_on_interval(self):
        from pr_watch.service import build_launchd_plist

        plist = build_launchd_plist(
            label="com.example.pr-watch.test",
            python_executable="/opt/pr-watch/bin/python",
            state_dir="/tmp/pr-watch-state",
            interval_seconds=120,
            stdout_path="/tmp/pr-watch.out.log",
            stderr_path="/tmp/pr-watch.err.log",
        )

        payload = plistlib.loads(plist.encode("utf-8"))
        self.assertEqual("com.example.pr-watch.test", payload["Label"])
        self.assertEqual(120, payload["StartInterval"])
        self.assertEqual("/tmp/pr-watch.out.log", payload["StandardOutPath"])
        self.assertEqual("/tmp/pr-watch.err.log", payload["StandardErrorPath"])
        self.assertEqual(
            [
                "/opt/pr-watch/bin/python",
                "-m",
                "pr_watch",
                "--state-dir",
                "/tmp/pr-watch-state",
                "service",
                "run-once",
            ],
            payload["ProgramArguments"],
        )

    def test_service_install_writes_plist_config_and_records_launchctl(self):
        from pr_watch.service import RecordingLaunchdRunner, install_launchd_service

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            plist_path = Path(tmpdir) / "LaunchAgents" / "com.example.pr-watch.test.plist"
            log_dir = Path(tmpdir) / "logs"
            runner = RecordingLaunchdRunner()

            result = install_launchd_service(
                state_dir=str(state_dir),
                interval_seconds=90,
                notification_mode="in_app",
                label="com.example.pr-watch.test",
                python_executable="/opt/pr-watch/bin/python",
                plist_path=plist_path,
                log_dir=log_dir,
                runner=runner,
            )

            self.assertEqual("installed", result.status)
            self.assertEqual(plist_path, result.plist_path)
            self.assertTrue(plist_path.exists())
            self.assertEqual("90", load_config(str(state_dir))["poll_interval_seconds"])
            self.assertEqual("in_app", load_config(str(state_dir))["notification_mode"])
            self.assertEqual(
                [
                    ["launchctl", "bootout", result.domain, str(plist_path)],
                    ["launchctl", "bootstrap", result.domain, str(plist_path)],
                ],
                runner.commands,
            )

    def test_service_run_once_skips_when_worker_lock_is_held(self):
        from pr_watch.service import run_service_once, single_worker_lock

        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            store.add_watch_repo("sendbird/ai-agent-js")

            with single_worker_lock(tmpdir) as acquired:
                self.assertTrue(acquired)
                result = run_service_once(
                    state_dir=tmpdir,
                    current_user_login="irene",
                    fixture=str(Path("tests/fixtures/prs.json").resolve()),
                    notification_mode="none",
                )

            self.assertEqual("locked", result.status)
            self.assertEqual(0, result.event_count)
            self.assertEqual([], store.list_events(include_done=True))

    def test_service_run_once_polls_all_watched_repos_with_fixture(self):
        from pr_watch.service import run_service_once

        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = Path(tmpdir) / "prs.json"
            fixture.write_text(
                """
                [
                  {
                    "owner": "alpha",
                    "repo": "one",
                    "number": 1,
                    "url": "https://github.com/alpha/one/pull/1",
                    "author": {"login": "bang9"},
                    "latestReviews": [
                      {"author": {"login": "irene"}, "state": "COMMENTED", "submittedAt": "2026-05-11T09:00:00Z"}
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z"
                  },
                  {
                    "owner": "beta",
                    "repo": "two",
                    "number": 2,
                    "url": "https://github.com/beta/two/pull/2",
                    "author": {"login": "bang9"},
                    "latestReviews": [
                      {"author": {"login": "irene"}, "state": "COMMENTED", "submittedAt": "2026-05-11T09:00:00Z"}
                    ],
                    "lastPushedAt": "2026-05-11T10:00:00Z",
                    "updatedAt": "2026-05-11T10:00:00Z"
                  }
                ]
                """,
                encoding="utf-8",
            )
            store = make_store(tmpdir)
            store.add_watch_repo("alpha/one")
            store.add_watch_repo("beta/two")

            result = run_service_once(
                state_dir=tmpdir,
                current_user_login="irene",
                fixture=str(fixture),
                notification_mode="in_app",
                sessions=[],
            )

            self.assertEqual("completed", result.status)
            self.assertEqual(["alpha/one", "beta/two"], [item.repo for item in result.repo_results])
            self.assertEqual([1, 1], [item.event_count for item in result.repo_results])
            self.assertEqual(2, result.event_count)
            self.assertEqual(
                ["alpha/one", "beta/two"],
                sorted(f"{item.repo_owner}/{item.repo_name}" for item in store.list_events(include_done=True)),
            )
            self.assertEqual(2, len(store.list_notifications(include_done=True)))

    def test_session_discovery_reads_claude_and_codex_indexes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            claude_log = home / ".claude" / "projects" / "repo" / "session.jsonl"
            claude_log.parent.mkdir(parents=True)
            claude_log.write_text(
                '{"sessionId":"claude-123","cwd":"/repo/ai-agent-js",'
                '"message":{"content":"Review https://github.com/sendbird/ai-agent-js/pull/1049"}}\n',
                encoding="utf-8",
            )
            codex_index = home / ".codex" / "session_index.jsonl"
            codex_index.parent.mkdir(parents=True)
            codex_index.write_text(
                '{"id":"codex-abc","cwd":"/repo/ai-agent-js","branch":"review/pr-1049",'
                '"title":"Review PR 1049","updated_at":"2026-05-11T10:00:00Z"}\n',
                encoding="utf-8",
            )

            sessions = discover_sessions(home)
            by_id = {session.session_id: session for session in sessions}

            self.assertEqual("claude", by_id["claude-123"].agent)
            self.assertIn(PR_URL, by_id["claude-123"].text)
            self.assertEqual("codex", by_id["codex-abc"].agent)
            self.assertEqual("review/pr-1049", by_id["codex-abc"].branch)

    def test_session_discovery_reads_codex_rollout_session_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            rollout = home / ".codex" / "sessions" / "2026" / "05" / "12"
            rollout.mkdir(parents=True)
            (rollout / "rollout-2026-05-12T13-14-27-codex-session-123.jsonl").write_text(
                '{"type":"session_meta","payload":{"id":"codex-session-123",'
                '"cwd":"/repo/claude-code","timestamp":"2026-05-12T04:14:27Z",'
                '"source":"exec"}}\n'
                '{"type":"event_msg","payload":{"type":"user_message","message":'
                '"Test https://github.com/AhyoungRyu/claude-code/pull/5"},'
                '"timestamp":"2026-05-12T04:30:00Z"}\n',
                encoding="utf-8",
            )

            sessions = discover_sessions(home)
            session = {item.session_id: item for item in sessions}["codex-session-123"]

            self.assertEqual("codex", session.agent)
            self.assertEqual("/repo/claude-code", session.cwd)
            self.assertEqual("2026-05-12T04:30:00Z", session.last_activity_at)
            self.assertIn("AhyoungRyu/claude-code/pull/5", session.text)


if __name__ == "__main__":
    unittest.main()
