import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from pr_watch.classifier import classify_pr
from pr_watch.cli import main
from pr_watch.config import load_config
from pr_watch.delivery import CommandResult, RecordingRunner, approve_event
from pr_watch.github import GH_PR_FIELDS, enrich_pull_request_metadata, poll_once
from pr_watch.models import SessionInfo
from pr_watch.notifications import resolve_notification_mode
from pr_watch.sessions import discover_sessions
from pr_watch.state import StateStore
from pr_watch.workflow import create_explicit_binding, route_event


PR_URL = "https://github.com/sendbird/ai-agent-js/pull/1049"


def make_store(tmpdir):
    return StateStore(Path(tmpdir) / "state.sqlite")


class PrWatchTests(unittest.TestCase):
    def test_github_polling_requests_pr_comments(self):
        self.assertIn("comments", GH_PR_FIELDS.split(","))

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

        enrich_pull_request_metadata(prs, "sendbird/ai-agent-js")

        self.assertEqual("2026-05-11T10:30:00Z", prs[0]["lastPushedAt"])

    def test_auto_notification_mode_prefers_in_app_for_app_hosts(self):
        self.assertEqual(
            "in_app",
            resolve_notification_mode("auto", host="conductor", platform_name="Darwin"),
        )
        self.assertEqual(
            "in_app",
            resolve_notification_mode("auto", host="codex_app", platform_name="Darwin"),
        )

    def test_auto_notification_mode_uses_desktop_for_plain_macos_terminal(self):
        self.assertEqual("desktop", resolve_notification_mode("auto", platform_name="Darwin"))

    def test_browser_notification_mode_is_legacy_alias_for_in_app(self):
        self.assertEqual("in_app", resolve_notification_mode("browser", platform_name="Darwin"))

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
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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

    def test_linked_github_issue_comment_becomes_actionable_pr_event(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "url": PR_URL,
                "author": {"login": "teammate"},
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
                                "author": {"login": "teammate"},
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
        self.assertEqual("teammate", linked_events[0].actor)
        self.assertIn("issue #321", linked_events[0].summary)
        self.assertEqual("github_issue", linked_events[0].payload["source"])

    def test_own_linked_issue_comments_are_muted(self):
        events = classify_pr(
            {
                "owner": "sendbird",
                "repo": "ai-agent-js",
                "number": 1049,
                "url": PR_URL,
                "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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

    def test_first_inferred_binding_requires_confirmation_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event = classify_pr(
                {
                    "owner": "sendbird",
                    "repo": "ai-agent-js",
                    "number": 1049,
                    "url": PR_URL,
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
                            "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
            self.assertIn("codex resume codex-abc", stored.recovery_command)
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
                    "author": {"login": "teammate"},
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
                    "author": {"login": "teammate"},
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
                '"Test https://github.com/AhyoungRyu/claude-code/pull/5"}}\n',
                encoding="utf-8",
            )

            sessions = discover_sessions(home)
            session = {item.session_id: item for item in sessions}["codex-session-123"]

            self.assertEqual("codex", session.agent)
            self.assertEqual("/repo/claude-code", session.cwd)
            self.assertIn("AhyoungRyu/claude-code/pull/5", session.text)


if __name__ == "__main__":
    unittest.main()
