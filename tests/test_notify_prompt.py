import inspect
import plistlib
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from pr_watch import delivery
from pr_watch.cli import main
from pr_watch.delivery import CommandResult, RecordingRunner
from pr_watch.host_adapter import sync_once
from pr_watch.models import ClassifiedEvent, PullRequestRef
from pr_watch.state import StateStore


PR_URL = "https://github.com/sendbird/ai-agent-js/pull/1049"


def make_store(tmpdir):
    return StateStore(Path(tmpdir) / "state.sqlite")


def make_event(number=1049, dedupe_suffix="main"):
    return ClassifiedEvent(
        pr=PullRequestRef(
            owner="sendbird",
            repo="ai-agent-js",
            number=number,
            url=f"https://github.com/sendbird/ai-agent-js/pull/{number}",
            title="Improve the tool runner",
            head_ref=f"review/pr-{number}",
        ),
        role="reviewer",
        event_type="author_push_after_review",
        summary=f"teammate pushed new commits to PR #{number}",
        actor="teammate",
        actionable=True,
        dedupe_key=f"notify-prompt:{number}:{dedupe_suffix}",
        payload={"lastPushedAt": "2026-05-11T10:00:00Z"},
    )


def make_confirmed_event(store, session_id="codex-confirmed", agent="codex", number=1049, host=None):
    binding = store.create_binding(
        repo_owner="sendbird",
        repo_name="ai-agent-js",
        pr_number=number,
        pr_url=f"https://github.com/sendbird/ai-agent-js/pull/{number}",
        role="reviewer",
        agent=agent,
        session_id=session_id,
        host=host,
        confirmed=True,
        active=True,
        confidence="high",
        confirmation_source="explicit_bind",
        evidence=["explicit user binding"],
    )
    event = store.upsert_event(
        make_event(number),
        status="pending",
        delivery_status="awaiting_approval",
        binding_id=binding.binding_id,
        confidence="high",
        evidence=["confirmed binding matched this PR and role"],
    )
    return event, binding


def create_conductor_db(path, session_id="conductor-session-1", claude_session_id="claude-abc"):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            create table sessions (
              id text primary key,
              status text default 'idle',
              claude_session_id text,
              unread_count integer default 0,
              created_at text default (datetime('now')),
              updated_at text default (datetime('now')),
              workspace_id text
            );
            create table workspaces (
              id text primary key,
              active_session_id text,
              unread integer default 0,
              created_at text default (datetime('now')),
              updated_at text default (datetime('now'))
            );
            create table session_messages (
              id text primary key,
              session_id text,
              role text,
              content text,
              created_at text default (datetime('now')),
              sent_at text,
              full_message text,
              is_resumable_message integer,
              queue_order integer
            );
            """
        )
        conn.execute(
            "insert into workspaces (id, active_session_id, unread) values (?, ?, 0)",
            ("workspace-1", session_id),
        )
        conn.execute(
            """
            insert into sessions (id, claude_session_id, unread_count, workspace_id)
            values (?, ?, 0, ?)
            """,
            (session_id, claude_session_id, "workspace-1"),
        )


class NotifyPromptTests(unittest.TestCase):
    def test_render_notify_prompt_is_notification_only_and_contains_guardrails(self):
        render_notify_prompt = getattr(delivery, "render_notify_prompt", None)
        self.assertIsNotNone(render_notify_prompt, "delivery.render_notify_prompt should exist")
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event, _binding = make_confirmed_event(store)

            prompt = render_notify_prompt(event)

        self.assertIn("PR Watch notification only.", prompt)
        self.assertIn("PR #1049", prompt)
        self.assertIn("sendbird/ai-agent-js", prompt)
        self.assertIn("teammate pushed new commits to PR #1049", prompt)
        self.assertIn("Actor: teammate", prompt)
        self.assertIn(PR_URL, prompt)
        lower_prompt = prompt.lower()
        for forbidden in [
            "do not run tools",
            "inspect files",
            "call github",
            "edit code",
            "post comments",
            "push",
            "external action",
        ]:
            self.assertIn(forbidden, lower_prompt)
        self.assertIn("ask the user", lower_prompt)
        self.assertIn("inspect", lower_prompt)

    def test_notify_prompt_event_resumes_idle_session_without_finalizing_event(self):
        notify_prompt_event = getattr(delivery, "notify_prompt_event", None)
        self.assertIsNotNone(notify_prompt_event, "delivery.notify_prompt_event should exist")
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event, _binding = make_confirmed_event(store)
            runner = RecordingRunner()

            result = notify_prompt_event(
                store,
                event.event_id,
                session_state="idle",
                runner=runner,
            )
            stored = store.get_event(event.event_id)

            self.assertEqual("notify_prompt_sent", result.action)
            self.assertEqual(1, len(runner.commands))
            self.assertEqual(["codex", "exec", "resume", "codex-confirmed"], runner.commands[0][:4])
            self.assertIn("PR Watch notification only.", runner.commands[0][4])
            self.assertEqual("pending", stored.status)
            self.assertEqual("awaiting_approval", stored.delivery_status)
            self.assertEqual([], store.list_queue())
            self.assertIsNotNone(store.get_host_sync(event.event_id, "notify_prompt", "codex-confirmed"))

    def test_notify_prompt_event_queues_when_session_state_is_unknown_without_finalizing_event(self):
        notify_prompt_event = getattr(delivery, "notify_prompt_event", None)
        self.assertIsNotNone(notify_prompt_event, "delivery.notify_prompt_event should exist")
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event, _binding = make_confirmed_event(store)
            runner = RecordingRunner()

            result = notify_prompt_event(
                store,
                event.event_id,
                session_state="unknown",
                runner=runner,
            )
            stored = store.get_event(event.event_id)
            queue = store.list_queue()

            self.assertEqual("notify_prompt_queued", result.action)
            self.assertEqual([], runner.commands)
            self.assertEqual(1, len(queue))
            self.assertEqual(event.event_id, queue[0].event_id)
            self.assertIn("PR Watch notification only.", queue[0].prompt)
            self.assertEqual("pending", stored.status)
            self.assertEqual("awaiting_approval", stored.delivery_status)

    def test_notify_prompt_event_is_deduped_per_event_and_session(self):
        notify_prompt_event = getattr(delivery, "notify_prompt_event", None)
        self.assertIsNotNone(notify_prompt_event, "delivery.notify_prompt_event should exist")
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event, _binding = make_confirmed_event(store)
            runner = RecordingRunner()

            first = notify_prompt_event(store, event.event_id, session_state="idle", runner=runner)
            second = notify_prompt_event(store, event.event_id, session_state="idle", runner=runner)

            self.assertEqual("notify_prompt_sent", first.action)
            self.assertEqual("notify_prompt_already_sent", second.action)
            self.assertEqual(1, len(runner.commands))
            self.assertEqual("pending", store.get_event(event.event_id).status)

    def test_notify_prompt_event_rejects_unsafe_or_unconfirmed_candidates(self):
        notify_prompt_event = getattr(delivery, "notify_prompt_event", None)
        self.assertIsNotNone(notify_prompt_event, "delivery.notify_prompt_event should exist")
        cases = [
            ("unconfirmed", {"confirmed": False}, {}, "pending", "awaiting_approval", "high"),
            ("inactive", {"active": False}, {}, "pending", "awaiting_approval", "high"),
            ("binding_low_confidence", {"confidence": "low"}, {}, "pending", "awaiting_approval", "high"),
            ("event_low_confidence", {}, {}, "pending", "awaiting_approval", "low"),
            ("first_inferred", {"confirmed": False}, {}, "needs_confirmation", "awaiting_first_binding_confirmation", "high"),
            ("rebind_confirmation", {"confirmed": False}, {}, "needs_confirmation", "awaiting_rebind_confirmation", "high"),
            ("already_delivered", {}, {}, "delivered", "delivered", "high"),
        ]
        for name, binding_overrides, _event_overrides, status, delivery_status, confidence in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmpdir:
                store = make_store(tmpdir)
                binding = store.create_binding(
                    repo_owner="sendbird",
                    repo_name="ai-agent-js",
                    pr_number=1049,
                    pr_url=PR_URL,
                    role="reviewer",
                    agent="codex",
                    session_id=f"codex-{name}",
                    confirmed=binding_overrides.get("confirmed", True),
                    active=binding_overrides.get("active", True),
                    confidence=binding_overrides.get("confidence", "high"),
                    confirmation_source="explicit_bind",
                    evidence=["test binding"],
                )
                event = store.upsert_event(
                    make_event(dedupe_suffix=name),
                    status=status,
                    delivery_status=delivery_status,
                    binding_id=binding.binding_id,
                    confidence=confidence,
                    evidence=["test event"],
                )
                runner = RecordingRunner()

                result = notify_prompt_event(store, event.event_id, session_state="idle", runner=runner)

                self.assertEqual("notify_prompt_skipped", result.action)
                self.assertEqual([], runner.commands)
                self.assertEqual([], store.list_queue())
                self.assertIsNone(store.get_host_sync(event.event_id, "notify_prompt", binding.session_id))

        with self.subTest(name="ambiguous_without_binding"), tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event = store.upsert_event(
                make_event(dedupe_suffix="ambiguous"),
                status="pending",
                delivery_status="ambiguous_session_candidates",
                binding_id=None,
                confidence="medium",
                evidence=["ambiguous"],
            )
            result = notify_prompt_event(store, event.event_id, session_state="idle", runner=RecordingRunner())

            self.assertEqual("notify_prompt_skipped", result.action)
            self.assertEqual([], store.list_queue())

    def test_host_sync_notify_prompt_confirmed_soft_triggers_codex_app_without_mirroring(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event, _binding = make_confirmed_event(store)
            runner = RecordingRunner()

            self.assertIn("notify_prompt_confirmed", inspect.signature(sync_once).parameters)
            result = sync_once(
                store,
                hosts=["codex-app"],
                notify_prompt_confirmed=True,
                runner=runner,
                session_state="idle",
            )

            self.assertEqual([], result.host_results)
            self.assertEqual([], result.trigger_results)
            self.assertEqual(["notify_prompt_sent"], [item.action for item in result.notify_prompt_results])
            self.assertEqual(1, len(runner.commands))
            self.assertEqual("pending", store.get_event(event.event_id).status)

    def test_host_sync_reports_notify_prompt_resume_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event, _binding = make_confirmed_event(store)
            runner = RecordingRunner(CommandResult(2, "", "resume failed"))

            result = sync_once(
                store,
                hosts=["codex-app"],
                notify_prompt_confirmed=True,
                runner=runner,
                session_state="idle",
            )

            self.assertEqual(["notify_prompt_failed"], [item.action for item in result.notify_prompt_results])
            self.assertEqual("resume failed", result.notify_prompt_results[0].message)
            self.assertEqual("pending", store.get_event(event.event_id).status)

    def test_host_sync_notify_prompt_confirmed_uses_conductor_codex_before_db_mirror(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            event, _binding = make_confirmed_event(
                store,
                session_id="conductor-session-1",
                host="conductor",
            )
            runner = RecordingRunner()
            conductor_codex = Path(tmpdir) / "conductor-codex"
            conductor_codex.write_text("#!/bin/sh\n", encoding="utf-8")

            self.assertIn("notify_prompt_confirmed", inspect.signature(sync_once).parameters)
            with patch("pr_watch.host_adapter.CONDUCTOR_CODEX_BINARY", conductor_codex, create=True):
                result = sync_once(
                    store,
                    hosts=["conductor"],
                    conductor_db_path=db_path,
                    notify_prompt_confirmed=True,
                    runner=runner,
                    session_state="idle",
                )

            self.assertEqual([], result.host_results)
            self.assertEqual(["notify_prompt_sent"], [item.action for item in result.notify_prompt_results])
            self.assertEqual(1, len(runner.commands))
            self.assertEqual([str(conductor_codex), "exec", "resume", "conductor-session-1"], runner.commands[0][:4])
            prompt = runner.commands[0][4]
            self.assertIn("PR Watch notification only.", prompt)
            self.assertIn("Do not inspect files/edit/comment unless user asks.", prompt)
            self.assertIn(f"Event id: {event.event_id}", prompt)
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("select count(*) from session_messages").fetchone()[0]
            self.assertEqual(0, count)
            self.assertIsNotNone(store.get_host_sync(event.event_id, "notify_prompt", "conductor-session-1"))

    def test_cli_notify_prompt_queues_soft_prompt_without_external_resume_when_state_unknown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event, _binding = make_confirmed_event(store)
            output = StringIO()

            try:
                with redirect_stdout(output):
                    code = main(
                        [
                            "--state-dir",
                            tmpdir,
                            "notify-prompt",
                            event.event_id,
                            "--session-state",
                            "unknown",
                        ]
                    )
            except SystemExit as exc:  # argparse before implementation would exit here
                self.fail(f"notify-prompt command should parse, got SystemExit {exc.code}")

            self.assertEqual(0, code)
            self.assertIn("notify_prompt_queued", output.getvalue())
            self.assertEqual(1, len(store.list_queue()))
            self.assertEqual("pending", store.get_event(event.event_id).status)

    def test_cli_host_sync_accepts_notify_prompt_confirmed_option(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            make_confirmed_event(store)
            output = StringIO()

            try:
                with redirect_stdout(output):
                    code = main(
                        [
                            "--state-dir",
                            tmpdir,
                            "host",
                            "sync-once",
                            "--host",
                            "codex-app",
                            "--notify-prompt-confirmed",
                            "--session-state",
                            "unknown",
                        ]
                    )
            except SystemExit as exc:  # argparse before implementation would exit here
                self.fail(f"--notify-prompt-confirmed should parse, got SystemExit {exc.code}")

            self.assertEqual(0, code)
            self.assertIn("notify-prompt", output.getvalue())
            self.assertIn("notify_prompt_queued", output.getvalue())
            self.assertEqual(1, len(store.list_queue()))

    def test_mcp_notify_prompt_session_queues_soft_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            event, _binding = make_confirmed_event(store)
            try:
                from pr_watch.mcp_server import notify_prompt_session
            except ImportError as exc:
                self.fail(f"notify_prompt_session should be exported by mcp_server: {exc}")

            result = notify_prompt_session(
                event_id=event.event_id,
                session_state="unknown",
                state_dir=tmpdir,
            )

            self.assertEqual("notify_prompt_queued", result["action"])
            self.assertEqual(1, len(store.list_queue()))
            self.assertEqual("pending", store.get_event(event.event_id).status)

    def test_launchd_plist_includes_notify_prompt_confirmed_when_host_sync_uses_fallback(self):
        from pr_watch.service import build_launchd_plist

        self.assertIn("notify_prompt_confirmed", inspect.signature(build_launchd_plist).parameters)
        self.assertIn("notify_prompt_session_state", inspect.signature(build_launchd_plist).parameters)
        plist = build_launchd_plist(
            label="com.example.pr-watch.test",
            python_executable="/opt/pr-watch/bin/python",
            state_dir="/tmp/pr-watch-state",
            interval_seconds=120,
            stdout_path="/tmp/pr-watch.out.log",
            stderr_path="/tmp/pr-watch.err.log",
            host_sync=True,
            host="codex-app",
            notify_prompt_confirmed=True,
        )

        payload = plistlib.loads(plist.encode("utf-8"))

        self.assertEqual(
            [
                "/opt/pr-watch/bin/python",
                "-m",
                "pr_watch",
                "--state-dir",
                "/tmp/pr-watch-state",
                "service",
                "run-once",
                "--host-sync",
                "--host",
                "codex-app",
                "--notify-prompt-confirmed",
                "--notify-prompt-session-state",
                "idle",
            ],
            payload["ProgramArguments"],
        )

    def test_launchd_plist_keeps_prompt_session_state_for_conductor_confirmation_prompts(self):
        from pr_watch.service import build_launchd_plist

        plist = build_launchd_plist(
            label="com.example.pr-watch.test",
            python_executable="/opt/pr-watch/bin/python",
            state_dir="/tmp/pr-watch-state",
            interval_seconds=120,
            stdout_path="/tmp/pr-watch.out.log",
            stderr_path="/tmp/pr-watch.err.log",
            host_sync=True,
            host="conductor",
            notify_prompt_session_state="unknown",
        )

        payload = plistlib.loads(plist.encode("utf-8"))

        self.assertIn("--notify-prompt-session-state", payload["ProgramArguments"])
        self.assertEqual(
            "unknown",
            payload["ProgramArguments"][payload["ProgramArguments"].index("--notify-prompt-session-state") + 1],
        )

    def test_service_host_sync_message_counts_confirmation_prompt_results(self):
        from pr_watch.host_adapter import HostEventResult
        from pr_watch.service import _host_sync_message

        message = _host_sync_message(
            [
                HostEventResult(
                    host="conductor",
                    event_id="evt_123",
                    action="confirmation_prompt_sent",
                    target_id="session-1",
                )
            ],
            trigger_count=0,
        )

        self.assertIn("confirmation_prompted=1", message)


if __name__ == "__main__":
    unittest.main()
