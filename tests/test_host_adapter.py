import json
import plistlib
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from pr_watch.cli import main
from pr_watch.conductor_adapter import (
    check_conductor_db,
    mirror_event_to_conductor,
)
from pr_watch.delivery import RecordingRunner
from pr_watch.host_adapter import sync_once
from pr_watch.mcp_server import host_status as mcp_host_status
from pr_watch.mcp_server import sync_host_once as mcp_sync_host_once
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
        dedupe_key=f"test:{number}:{dedupe_suffix}",
        payload={"lastPushedAt": "2026-05-11T10:00:00Z"},
    )


def make_inbox_item(
    store,
    number=1049,
    dedupe_suffix="main",
    binding_id=None,
    status="pending",
    delivery_status="awaiting_approval",
    confidence="high",
):
    return store.upsert_event(
        make_event(number, dedupe_suffix),
        status=status,
        delivery_status=delivery_status,
        binding_id=binding_id,
        confidence=confidence,
        evidence=["test event"],
    )


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


class HostAdapterTests(unittest.TestCase):
    def test_conductor_status_reports_missing_and_available_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.sqlite"
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)

            missing_status = check_conductor_db(missing)
            available_status = check_conductor_db(db_path)

            self.assertFalse(missing_status.available)
            self.assertIn("not found", missing_status.message)
            self.assertTrue(available_status.available)
            self.assertEqual("available", available_status.status)

    def test_conductor_mirror_matches_session_id_and_marks_unread_with_dedupe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path, session_id="conductor-session-1")
            store = make_store(tmpdir)
            binding = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=True,
                confirmation_source="explicit_bind",
                evidence=["explicit user binding"],
            )
            event = make_inbox_item(store, binding_id=binding.binding_id)

            first = mirror_event_to_conductor(db_path, event, binding)
            second = mirror_event_to_conductor(db_path, event, binding)

            self.assertEqual("mirrored", first.action)
            self.assertEqual("already_synced", second.action)
            self.assertEqual("conductor-session-1", first.session_id)
            with sqlite3.connect(db_path) as conn:
                messages = conn.execute("select role, content from session_messages").fetchall()
                session_unread = conn.execute(
                    "select unread_count from sessions where id = ?",
                    ("conductor-session-1",),
                ).fetchone()[0]
                workspace_unread = conn.execute(
                    "select unread from workspaces where id = 'workspace-1'"
                ).fetchone()[0]

            self.assertEqual(1, len(messages))
            self.assertEqual("assistant", messages[0][0])
            self.assertIn(f"pr-watch:event_id={event.event_id}", messages[0][1])
            self.assertIn("synthetic", messages[0][1])
            payload = json.loads(messages[0][1])
            self.assertEqual("assistant", payload["type"])
            self.assertEqual("assistant", payload["message"]["role"])
            self.assertEqual("text", payload["message"]["content"][0]["type"])
            self.assertEqual(1, session_unread)
            self.assertEqual(1, workspace_unread)

    def test_conductor_mirror_can_match_claude_session_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path, session_id="conductor-session-1", claude_session_id="claude-abc")
            store = make_store(tmpdir)
            binding = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="claude",
                session_id="claude-abc",
                host="conductor",
                confirmed=True,
                confirmation_source="explicit_bind",
                evidence=["explicit user binding"],
            )
            event = make_inbox_item(store, binding_id=binding.binding_id)

            result = mirror_event_to_conductor(db_path, event, binding)

            self.assertEqual("mirrored", result.action)
            self.assertEqual("conductor-session-1", result.session_id)

    def test_host_sync_records_state_dedupe_for_conductor_mirrors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            binding = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=True,
                confirmation_source="explicit_bind",
                evidence=["explicit user binding"],
            )
            event = make_inbox_item(store, binding_id=binding.binding_id)

            first = sync_once(store, hosts=["conductor"], conductor_db_path=db_path)
            second = sync_once(store, hosts=["conductor"], conductor_db_path=db_path)

            self.assertEqual(["mirrored"], [item.action for item in first.host_results])
            self.assertEqual(["already_synced"], [item.action for item in second.host_results])
            self.assertIsNotNone(store.get_host_sync(event.event_id, "conductor", "conductor-session-1"))
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("select count(*) from session_messages").fetchone()[0]
            self.assertEqual(1, count)

    def test_trigger_confirmed_only_resumes_confirmed_high_confidence_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store(tmpdir)
            confirmed = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="codex-confirmed",
                confirmed=True,
                confirmation_source="explicit_bind",
                evidence=["explicit user binding"],
            )
            unconfirmed = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1050,
                pr_url="https://github.com/sendbird/ai-agent-js/pull/1050",
                role="reviewer",
                agent="codex",
                session_id="codex-unconfirmed",
                confirmed=False,
                confirmation_source="inferred_candidate",
                evidence=["inferred candidate"],
            )
            confirmed_event = make_inbox_item(
                store,
                number=1049,
                dedupe_suffix="confirmed",
                binding_id=confirmed.binding_id,
            )
            unconfirmed_event = make_inbox_item(
                store,
                number=1050,
                dedupe_suffix="unconfirmed",
                binding_id=unconfirmed.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
            )
            ambiguous_event = make_inbox_item(
                store,
                number=1051,
                dedupe_suffix="ambiguous",
                binding_id=None,
                delivery_status="ambiguous_session_candidates",
                confidence="medium",
            )
            low_event = make_inbox_item(
                store,
                number=1052,
                dedupe_suffix="low",
                binding_id=None,
                delivery_status="inbox_only",
                confidence="low",
            )
            runner = RecordingRunner()

            result = sync_once(
                store,
                hosts=[],
                trigger_confirmed=True,
                runner=runner,
                session_state="idle",
            )

            self.assertEqual(["delivered"], [item.action for item in result.trigger_results])
            self.assertEqual(1, len(runner.commands))
            self.assertEqual("codex-confirmed", runner.commands[0][2])
            self.assertEqual("delivered", store.get_event(confirmed_event.event_id).status)
            self.assertEqual("needs_confirmation", store.get_event(unconfirmed_event.event_id).status)
            self.assertEqual("pending", store.get_event(ambiguous_event.event_id).status)
            self.assertEqual("pending", store.get_event(low_event.event_id).status)

    def test_cli_host_status_reports_conductor_and_codex_app_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            output = StringIO()

            with redirect_stdout(output):
                code = main(["--state-dir", tmpdir, "host", "status", "--conductor-db", str(db_path)])

            text = output.getvalue()
            self.assertEqual(0, code)
            self.assertIn("conductor: available", text)
            self.assertIn("codex-app: no_push_support", text)

    def test_cli_host_sync_once_mirrors_pending_event_to_conductor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            binding = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=True,
                confirmation_source="explicit_bind",
                evidence=["explicit user binding"],
            )
            make_inbox_item(store, binding_id=binding.binding_id)
            output = StringIO()

            with redirect_stdout(output):
                code = main(
                    [
                        "--state-dir",
                        tmpdir,
                        "host",
                        "sync-once",
                        "--host",
                        "conductor",
                        "--conductor-db",
                        str(db_path),
                    ]
                )

            self.assertEqual(0, code)
            self.assertIn("mirrored", output.getvalue())
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("select count(*) from session_messages").fetchone()[0]
            self.assertEqual(1, count)

    def test_mcp_host_tools_report_status_and_sync_conductor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            binding = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=True,
                confirmation_source="explicit_bind",
                evidence=["explicit user binding"],
            )
            make_inbox_item(store, binding_id=binding.binding_id)

            status = mcp_host_status(conductor_db_path=str(db_path))
            result = mcp_sync_host_once(
                host="conductor",
                conductor_db_path=str(db_path),
                state_dir=tmpdir,
            )

            self.assertEqual("available", status["conductor"]["status"])
            self.assertEqual("no_push_support", status["codex_app"]["status"])
            self.assertEqual("mirrored", result["host_results"][0]["action"])
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("select count(*) from session_messages").fetchone()[0]
            self.assertEqual(1, count)

    def test_launchd_service_can_run_host_sync_after_polling(self):
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
            conductor_db_path="/tmp/conductor.sqlite",
            trigger_confirmed=True,
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
                "conductor",
                "--conductor-db",
                "/tmp/conductor.sqlite",
                "--trigger-confirmed",
            ],
            payload["ProgramArguments"],
        )

    def test_service_run_once_can_sync_conductor_after_polling(self):
        from pr_watch.service import run_service_once

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
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
            store.add_watch_repo("sendbird/ai-agent-js")
            store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=True,
                confirmation_source="explicit_bind",
                evidence=["explicit user binding"],
            )

            result = run_service_once(
                state_dir=tmpdir,
                current_user_login="irene",
                fixture=str(fixture),
                notification_mode="none",
                sessions=[],
                host_sync=True,
                host="conductor",
                conductor_db_path=db_path,
            )

            self.assertEqual("completed", result.status)
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("select count(*) from session_messages").fetchone()[0]
            self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
