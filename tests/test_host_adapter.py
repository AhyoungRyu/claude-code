import json
import plistlib
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from pr_watch.cli import main
from pr_watch.conductor_adapter import (
    check_conductor_db,
    mirror_confirmation_to_conductor,
    mirror_event_to_conductor,
)
from pr_watch.delivery import RecordingRunner
from pr_watch.host_adapter import sync_once
from pr_watch.mcp_server import host_status as mcp_host_status
from pr_watch.mcp_server import sync_host_once as mcp_sync_host_once
from pr_watch.models import ClassifiedEvent, PullRequestRef
from pr_watch.state import StateStore
from pr_watch.workflow import confirm_binding_for_event


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
        summary=f"bang9 pushed new commits to PR #{number}",
        actor="bang9",
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
              turn_id text,
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


def add_conductor_session(path, session_id, claude_session_id=None, workspace_id=None):
    workspace_id = workspace_id or f"workspace-{session_id}"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into workspaces (id, active_session_id, unread) values (?, ?, 0)",
            (workspace_id, session_id),
        )
        conn.execute(
            """
            insert into sessions (id, claude_session_id, unread_count, workspace_id)
            values (?, ?, 0, ?)
            """,
            (session_id, claude_session_id, workspace_id),
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
                messages = conn.execute(
                    """
                    select id, role, content, created_at, sent_at, turn_id, queue_order
                    from session_messages
                    order by created_at, role desc
                    """
                ).fetchall()
                session_unread = conn.execute(
                    "select unread_count from sessions where id = ?",
                    ("conductor-session-1",),
                ).fetchone()[0]
                workspace_unread = conn.execute(
                    "select unread from workspaces where id = 'workspace-1'"
                ).fetchone()[0]

            self.assertEqual(2, len(messages))
            user = next(row for row in messages if row[1] == "user")
            assistant = next(row for row in messages if row[1] == "assistant")
            self.assertEqual(user[0], user[5])
            self.assertEqual(user[0], assistant[5])
            self.assertIsNone(user[6])
            self.assertIn(f"pr-watch:event_id={event.event_id}", user[2])
            self.assertIn("Suggested replies:", user[2])
            self.assertEqual(assistant[3], assistant[4])
            payload = json.loads(assistant[2])
            self.assertEqual("assistant", payload["type"])
            self.assertEqual("conductor-session-1", payload["session_id"])
            self.assertEqual("assistant", payload["message"]["role"])
            self.assertEqual("Inspect update", payload["suggested_replies"][0]["label"])
            self.assertNotIn("model", payload["message"])
            self.assertEqual("text", payload["message"]["content"][0]["type"])
            self.assertEqual(1, session_unread)
            self.assertEqual(1, workspace_unread)

    def test_conductor_confirmation_synthetic_turns_are_not_host_queued(self):
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
                confirmed=False,
                active=False,
                confirmation_source="inferred_candidate",
                evidence=["candidate binding"],
            )
            event = make_inbox_item(
                store,
                binding_id=binding.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
            )

            result = mirror_confirmation_to_conductor(db_path, event, binding)

            self.assertEqual("confirmation_requested", result.action)
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    """
                    select role, queue_order, is_resumable_message
                    from session_messages
                    order by role desc
                    """
                ).fetchall()
            self.assertEqual(["user", "assistant"], [row[0] for row in rows])
            self.assertTrue(all(row[1] is None for row in rows))
            self.assertTrue(all(row[2] == 0 for row in rows))

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
            with sqlite3.connect(db_path) as conn:
                content = conn.execute("select content from session_messages").fetchone()[0]
            assistant_content = conn.execute(
                "select content from session_messages where role = 'assistant'"
            ).fetchone()[0]
            self.assertEqual("claude-abc", json.loads(assistant_content)["session_id"])

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
            self.assertEqual(2, count)

    def test_conductor_mirror_repairs_hidden_legacy_synthetic_rows(self):
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
            marker = f"pr-watch:event_id={event.event_id}"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    insert into session_messages (id, session_id, role, content, created_at, sent_at)
                    values ('legacy-hidden', 'conductor-session-1', 'assistant', ?, '2026-05-14T00:00:00Z', '2026-05-14T00:00:00Z')
                    """,
                    (json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": marker}]}}),),
                )

            result = mirror_event_to_conductor(db_path, event, binding)

            self.assertEqual("mirrored", result.action)
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    """
                    select id, role, turn_id, content from session_messages
                    where content like ?
                    order by created_at
                    """,
                    (f"%{marker}%",),
                ).fetchall()
            self.assertEqual(2, len(rows))
            self.assertEqual("legacy-hidden", rows[0][0])
            visible_user = next(row for row in rows if row[1] == "user")
            self.assertEqual(visible_user[0], visible_user[2])
            with sqlite3.connect(db_path) as conn:
                visible_assistant = conn.execute(
                    """
                    select id, role, turn_id from session_messages
                    where role = 'assistant' and turn_id = ?
                    """,
                    (visible_user[0],),
                ).fetchone()
            self.assertIsNotNone(visible_assistant)

    def test_conductor_mirror_repairs_visible_rows_without_suggested_replies(self):
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
            marker = f"pr-watch:event_id={event.event_id}"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    insert into session_messages (
                      id, session_id, role, content, created_at, sent_at, turn_id, queue_order
                    ) values (
                      'legacy-visible-turn', 'conductor-session-1', 'user', ?, '2026-05-14T00:00:00Z',
                      '2026-05-14T00:00:00Z', 'legacy-visible-turn', 1
                    )
                    """,
                    (f"PR Watch notification only.\n{marker}",),
                )
                conn.execute(
                    """
                    insert into session_messages (
                      id, session_id, role, content, created_at, sent_at, turn_id
                    ) values (
                      'legacy-visible-assistant', 'conductor-session-1', 'assistant', ?,
                      '2026-05-14T00:00:00Z', '2026-05-14T00:00:00Z', 'legacy-visible-turn'
                    )
                    """,
                    (json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": marker}]}}),),
                )

            result = mirror_event_to_conductor(db_path, event, binding)

            self.assertEqual("mirrored", result.action)
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    """
                    select id, role, turn_id, content from session_messages
                    where content like ?
                    order by created_at
                    """,
                    (f"%{marker}%",),
                ).fetchall()
            self.assertEqual(3, len(rows))
            modern_user = next(row for row in rows if "Suggested replies:" in row[3])
            self.assertNotEqual("legacy-visible-turn", modern_user[0])
            self.assertEqual(modern_user[0], modern_user[2])

    def test_confirm_binding_for_event_mirrors_current_event_without_triggering_resume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=False,
                confirmation_source="inferred_candidate",
                evidence=["inferred candidate"],
            )
            event = make_inbox_item(
                store,
                binding_id=candidate.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
            )

            result = confirm_binding_for_event(
                store,
                event.event_id,
                mirror_now=True,
                conductor_db_path=db_path,
                trigger=False,
                runner=RecordingRunner(),
            )

            self.assertEqual("confirmed_binding", result["action"])
            self.assertEqual(["mirrored"], [item.action for item in result["host_sync"].host_results])
            self.assertEqual([], store.list_queue())
            self.assertEqual("pending", store.get_event(event.event_id).status)
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("select count(*) from session_messages").fetchone()[0]
            self.assertEqual(2, count)

    def test_host_sync_mirrors_confirmation_to_visible_conductor_turn_when_state_unknown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=False,
                active=False,
                confirmation_source="rebind_candidate",
                evidence=["new candidate"],
            )
            event = make_inbox_item(
                store,
                binding_id=candidate.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_rebind_confirmation",
            )

            first = sync_once(store, hosts=["conductor"], conductor_db_path=db_path)
            second = sync_once(store, hosts=["conductor"], conductor_db_path=db_path)

            self.assertEqual(["confirmation_requested"], [item.action for item in first.host_results])
            self.assertEqual(["confirmation_already_requested"], [item.action for item in second.host_results])
            self.assertIsNotNone(
                store.get_host_sync(event.event_id, "conductor_confirmation", candidate.session_id)
            )
            queue = store.list_queue()
            self.assertEqual(0, len(queue))
            self.assertEqual("needs_confirmation", store.get_event(event.event_id).status)
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute("select content, created_at, sent_at from session_messages").fetchall()
            self.assertEqual(2, len(rows))
            self.assertTrue(any("Suggested replies:" in row[0] for row in rows))
            self.assertTrue(any(f"pr-watch:confirm_event_id={event.event_id}" in row[0] for row in rows))

    def test_host_sync_does_not_remirror_confirmation_when_host_sync_exists_without_visible_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=False,
                active=False,
                confirmation_source="inferred_candidate",
                evidence=["candidate"],
            )
            event = make_inbox_item(
                store,
                binding_id=candidate.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
            )
            store.upsert_host_sync(
                event.event_id,
                "conductor_confirmation",
                candidate.session_id,
                "confirmation_requested",
                external_id="legacy-visible-turn",
            )

            result = sync_once(store, hosts=["conductor"], conductor_db_path=db_path)

            self.assertEqual(["confirmation_already_requested"], [item.action for item in result.host_results])
            self.assertEqual("needs_confirmation", store.get_event(event.event_id).status)
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("select count(*) from session_messages").fetchone()[0]
            self.assertEqual(0, count)

    def test_host_sync_does_not_remirror_legacy_confirmation_prompt_when_host_sync_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=False,
                active=False,
                confirmation_source="inferred_candidate",
                evidence=["candidate"],
            )
            event = make_inbox_item(
                store,
                binding_id=candidate.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
            )
            marker = f"pr-watch:confirm_event_id={event.event_id}"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    insert into session_messages (
                      id, session_id, role, content, created_at, sent_at, turn_id, queue_order
                    ) values (
                      'legacy-confirmation-turn', 'conductor-session-1', 'user', ?,
                      '2026-05-14T00:00:00Z', '2026-05-14T00:00:00Z',
                      'legacy-confirmation-turn', 1
                    )
                    """,
                    (f"PR Watch confirmation.\n{marker}\npr-watch:prompt_version=1",),
                )
                conn.execute(
                    """
                    insert into session_messages (
                      id, session_id, role, content, created_at, sent_at, turn_id
                    ) values (
                      'legacy-confirmation-assistant', 'conductor-session-1', 'assistant', ?,
                      '2026-05-14T00:00:00Z', '2026-05-14T00:00:00Z',
                      'legacy-confirmation-turn'
                    )
                    """,
                    (json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": marker}]}}),),
                )
            store.upsert_host_sync(
                event.event_id,
                "conductor_confirmation",
                candidate.session_id,
                "confirmation_requested",
                external_id="legacy-confirmation-turn",
            )

            result = sync_once(store, hosts=["conductor"], conductor_db_path=db_path)

            self.assertEqual(["confirmation_already_requested"], [item.action for item in result.host_results])
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("select count(*) from session_messages").fetchone()[0]
            self.assertEqual(2, count)

    def test_host_sync_allows_confirmation_prompt_for_new_candidate_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path, session_id="conductor-session-1")
            add_conductor_session(db_path, "conductor-session-2")
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-2",
                host="conductor",
                confirmed=False,
                active=False,
                confirmation_source="rebind_candidate",
                evidence=["new candidate"],
            )
            event = make_inbox_item(
                store,
                binding_id=candidate.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_rebind_confirmation",
            )
            store.upsert_host_sync(
                event.event_id,
                "conductor_confirmation",
                "conductor-session-1",
                "confirmation_requested",
                external_id="old-confirmation-turn",
            )

            result = sync_once(store, hosts=["conductor"], conductor_db_path=db_path)

            self.assertEqual(["confirmation_requested"], [item.action for item in result.host_results])
            self.assertIsNotNone(
                store.get_host_sync(event.event_id, "conductor_confirmation", "conductor-session-2")
            )
            with sqlite3.connect(db_path) as conn:
                sessions = conn.execute(
                    "select distinct session_id from session_messages order by session_id"
                ).fetchall()
            self.assertEqual([("conductor-session-2",)], sessions)

    def test_conductor_confirmation_turn_has_clear_replies_and_button_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=False,
                active=False,
                confirmation_source="inferred_candidate",
                evidence=["candidate"],
            )
            event = make_inbox_item(
                store,
                binding_id=candidate.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
            )

            result = sync_once(store, hosts=["conductor"], conductor_db_path=db_path)

            self.assertEqual(["confirmation_requested"], [item.action for item in result.host_results])
            with sqlite3.connect(db_path) as conn:
                user_content = conn.execute(
                    "select content from session_messages where role = 'user'"
                ).fetchone()[0]
                assistant_content = conn.execute(
                    "select content from session_messages where role = 'assistant'"
                ).fetchone()[0]
            self.assertIn("PR Watch: Is this the right session", user_content)
            self.assertIn("sendbird/ai-agent-js#1049", user_content)
            self.assertIn("bang9 pushed new commits to PR #1049", user_content)
            self.assertIn("Suggested replies:", user_content)
            self.assertIn("Confirm this session", user_content)
            self.assertIn("Confirm and mark handled", user_content)
            self.assertIn("Not this session", user_content)
            self.assertIn("Ignore this update", user_content)
            self.assertNotIn("requested attention", user_content)
            self.assertNotIn("Link:", user_content)
            self.assertNotIn("Event id:", user_content)
            self.assertNotIn("pr-watch:", user_content)
            self.assertNotIn("No PR inspection", user_content)
            self.assertIn("Do not run tools or read files", user_content)
            self.assertIn("unless the user chooses Confirm this session", user_content)
            payload = json.loads(assistant_content)
            labels = [item["label"] for item in payload["suggested_replies"]]
            self.assertEqual(
                ["Confirm this session", "Confirm and mark handled", "Not this session", "Ignore this update"],
                labels,
            )
            actions = [item["action"] for item in payload["suggested_replies"]]
            self.assertIn("confirm_binding_and_mark_handled", actions)
            self.assertEqual("2", payload["pr_watch"]["prompt_version"])
            assistant_text = payload["message"]["content"][0]["text"]
            self.assertIn("I found a likely PR Watch session match", assistant_text)
            self.assertIn("I will wait for your choice before running tools or reading files", assistant_text)

    def test_conductor_confirmed_update_turn_has_inspection_replies(self):
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
                active=True,
                confirmation_source="explicit_bind",
                evidence=["confirmed"],
            )
            make_inbox_item(store, binding_id=binding.binding_id)

            result = sync_once(store, hosts=["conductor"], conductor_db_path=db_path)

            self.assertEqual(["mirrored"], [item.action for item in result.host_results])
            with sqlite3.connect(db_path) as conn:
                user_content = conn.execute(
                    "select content from session_messages where role = 'user'"
                ).fetchone()[0]
                assistant_content = conn.execute(
                    "select content from session_messages where role = 'assistant'"
                ).fetchone()[0]
            self.assertIn("PR Watch: PR #1049 has an update", user_content)
            self.assertIn("Inspect update", user_content)
            self.assertIn("Queue for later", user_content)
            self.assertIn("Ignore this update", user_content)
            self.assertIn("pr-watch:prompt_version=2", user_content)
            payload = json.loads(assistant_content)
            labels = [item["label"] for item in payload["suggested_replies"]]
            self.assertEqual(["Inspect update", "Queue for later", "Ignore this update"], labels)
            self.assertEqual("2", payload["pr_watch"]["prompt_version"])

    def test_host_sync_prefers_visible_conductor_turn_before_codex_resume_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=False,
                active=False,
                confirmation_source="rebind_candidate",
                evidence=["new candidate"],
            )
            event = make_inbox_item(
                store,
                binding_id=candidate.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_rebind_confirmation",
            )
            runner = RecordingRunner()
            conductor_codex = Path(tmpdir) / "conductor-codex"
            conductor_codex.write_text("#!/bin/sh\n", encoding="utf-8")

            with patch("pr_watch.host_adapter.CONDUCTOR_CODEX_BINARY", conductor_codex, create=True):
                result = sync_once(
                    store,
                    hosts=["conductor"],
                    conductor_db_path=db_path,
                    runner=runner,
                    session_state="idle",
                )

            self.assertEqual(["confirmation_requested"], [item.action for item in result.host_results])
            self.assertEqual([], runner.commands)
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute("select role, content, turn_id from session_messages order by role desc").fetchall()
            self.assertEqual(2, len(rows))
            self.assertEqual({"assistant", "user"}, {row[0] for row in rows})
            self.assertTrue(any(f"pr-watch:confirm_event_id={event.event_id}" in row[1] for row in rows))
            self.assertIsNotNone(
                store.get_host_sync(event.event_id, "conductor_confirmation", candidate.session_id)
            )

    def test_host_sync_sends_confirmation_prompt_even_when_conductor_db_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.sqlite"
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=False,
                active=False,
                confirmation_source="rebind_candidate",
                evidence=["new candidate"],
            )
            event = make_inbox_item(
                store,
                binding_id=candidate.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_rebind_confirmation",
            )
            runner = RecordingRunner()
            conductor_codex = Path(tmpdir) / "conductor-codex"
            conductor_codex.write_text("#!/bin/sh\n", encoding="utf-8")

            with patch("pr_watch.host_adapter.CONDUCTOR_CODEX_BINARY", conductor_codex, create=True):
                result = sync_once(
                    store,
                    hosts=["conductor"],
                    conductor_db_path=db_path,
                    runner=runner,
                    session_state="idle",
                )

            self.assertEqual(["confirmation_prompt_sent"], [item.action for item in result.host_results])
            self.assertEqual(1, len(runner.commands))
            self.assertEqual([str(conductor_codex), "exec", "resume", "conductor-session-1"], runner.commands[0][:4])
            self.assertIsNotNone(
                store.get_host_sync(event.event_id, "conductor_confirmation_prompt", candidate.session_id)
            )

    def test_host_sync_retries_failed_confirmation_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=False,
                active=False,
                confirmation_source="rebind_candidate",
                evidence=["new candidate"],
            )
            event = make_inbox_item(
                store,
                binding_id=candidate.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_rebind_confirmation",
            )
            store.upsert_host_sync(
                event.event_id,
                "conductor_confirmation_prompt",
                candidate.session_id,
                "failed",
                error="stdin is not a terminal",
            )
            runner = RecordingRunner()
            conductor_codex = Path(tmpdir) / "conductor-codex"
            conductor_codex.write_text("#!/bin/sh\n", encoding="utf-8")

            with patch("pr_watch.host_adapter.CONDUCTOR_CODEX_BINARY", conductor_codex, create=True):
                result = sync_once(
                    store,
                    hosts=["conductor"],
                    conductor_db_path=db_path,
                    runner=runner,
                    session_state="idle",
                )

            self.assertEqual(["confirmation_requested"], [item.action for item in result.host_results])
            self.assertEqual([], runner.commands)
            self.assertEqual("failed", store.get_host_sync(
                event.event_id,
                "conductor_confirmation_prompt",
                candidate.session_id,
            ).status)
            self.assertIsNotNone(store.get_host_sync(
                event.event_id,
                "conductor_confirmation",
                candidate.session_id,
            ))

    def test_confirm_after_confirmation_prompt_can_still_mirror_actual_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conductor.sqlite"
            create_conductor_db(db_path)
            store = make_store(tmpdir)
            candidate = store.create_binding(
                repo_owner="sendbird",
                repo_name="ai-agent-js",
                pr_number=1049,
                pr_url=PR_URL,
                role="reviewer",
                agent="codex",
                session_id="conductor-session-1",
                host="conductor",
                confirmed=False,
                active=False,
                confirmation_source="inferred_candidate",
                evidence=["candidate"],
            )
            event = make_inbox_item(
                store,
                binding_id=candidate.binding_id,
                status="needs_confirmation",
                delivery_status="awaiting_first_binding_confirmation",
            )

            sync_once(store, hosts=["conductor"], conductor_db_path=db_path)
            confirm_binding_for_event(
                store,
                event.event_id,
                mirror_now=True,
                conductor_db_path=db_path,
            )

            with sqlite3.connect(db_path) as conn:
                contents = [row[0] for row in conn.execute("select content from session_messages order by created_at")]
            self.assertEqual(4, len(contents))
            self.assertTrue(any(f"pr-watch:event_id={event.event_id}" in item for item in contents))
            self.assertTrue(any(f"pr-watch:confirm_event_id={event.event_id}" in item for item in contents))
            self.assertEqual(0, len(store.list_queue()))

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
            self.assertEqual("codex-confirmed", runner.commands[0][3])
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
            self.assertEqual(2, count)

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
            self.assertEqual(2, count)

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
            self.assertIn("mirrored=1", result.message)
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("select count(*) from session_messages").fetchone()[0]
            self.assertEqual(2, count)


if __name__ == "__main__":
    unittest.main()
