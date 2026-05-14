from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .models import Binding, InboxItem
from .util import utc_now


DEFAULT_CONDUCTOR_DB_PATH = (
    Path.home() / "Library" / "Application Support" / "com.conductor.app" / "conductor.db"
)

REQUIRED_COLUMNS = {
    "sessions": {"id", "claude_session_id", "unread_count", "updated_at", "workspace_id"},
    "workspaces": {"id", "active_session_id", "unread", "updated_at"},
    "session_messages": {
        "id",
        "session_id",
        "role",
        "content",
        "created_at",
        "sent_at",
        "turn_id",
        "queue_order",
    },
}


@dataclass(frozen=True)
class ConductorStatus:
    status: str
    db_path: Path
    available: bool
    message: str = ""


@dataclass(frozen=True)
class ConductorMirrorResult:
    action: str
    event_id: str
    session_id: str = ""
    message_id: str = ""
    message: str = ""


def conductor_db_path(path: Optional[str | Path] = None) -> Path:
    return Path(path).expanduser() if path else DEFAULT_CONDUCTOR_DB_PATH


def check_conductor_db(path: Optional[str | Path] = None) -> ConductorStatus:
    db_path = conductor_db_path(path)
    if not db_path.exists():
        return ConductorStatus("missing", db_path, False, f"Conductor DB not found: {db_path}")

    try:
        with _connect_readonly(db_path) as conn:
            missing = _missing_schema(conn)
    except sqlite3.Error as exc:
        return ConductorStatus("unavailable", db_path, False, str(exc))

    if missing:
        return ConductorStatus(
            "schema_mismatch",
            db_path,
            False,
            "missing required Conductor table/column(s): " + ", ".join(missing),
        )
    return ConductorStatus("available", db_path, True, "Conductor private SQLite surface is available")


def mirror_event_to_conductor(
    path: Optional[str | Path],
    event: InboxItem,
    binding: Binding,
) -> ConductorMirrorResult:
    return _mirror_message_to_conductor(
        path,
        event,
        binding,
        marker=f"pr-watch:event_id={event.event_id}",
        action="mirrored",
        already_action="already_synced",
        already_message="event already mirrored into Conductor",
        success_message="event mirrored into Conductor as a visible synthetic turn",
        content_factory=render_conductor_message_turn,
    )


def mirror_confirmation_to_conductor(
    path: Optional[str | Path],
    event: InboxItem,
    binding: Binding,
) -> ConductorMirrorResult:
    return _mirror_message_to_conductor(
        path,
        event,
        binding,
        marker=f"pr-watch:confirm_event_id={event.event_id}",
        action="confirmation_requested",
        already_action="confirmation_already_requested",
        already_message="binding confirmation request already mirrored into Conductor",
        success_message="binding confirmation request mirrored into Conductor as a visible synthetic turn",
        content_factory=render_conductor_confirmation_turn,
    )


def _mirror_message_to_conductor(
    path: Optional[str | Path],
    event: InboxItem,
    binding: Binding,
    marker: str,
    action: str,
    already_action: str,
    already_message: str,
    success_message: str,
    content_factory: Callable[[InboxItem], tuple[str, str]],
) -> ConductorMirrorResult:
    status = check_conductor_db(path)
    if not status.available:
        return ConductorMirrorResult(status.status, event.event_id, message=status.message)

    db_path = status.db_path
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            session = _find_session(conn, binding.session_id)
            if session is None:
                return ConductorMirrorResult(
                    "session_not_found",
                    event.event_id,
                    message=f"no Conductor session matched {binding.session_id}",
                )

            session_id = str(session["id"])
            existing_message_id = _find_existing_visible_message_id(conn, session_id, marker)
            if existing_message_id:
                return ConductorMirrorResult(
                    already_action,
                    event.event_id,
                    session_id=session_id,
                    message_id=existing_message_id,
                    message=already_message,
                )

            message_id = str(uuid.uuid4())
            turn_id = str(uuid.uuid4())
            created_at = utc_now()
            user_content, assistant_text = content_factory(event)
            assistant_content = _render_assistant_payload(
                event,
                assistant_text,
                session_id=binding.session_id,
                message_id=message_id,
            )
            conn.execute(
                """
                insert into session_messages (
                  id, session_id, role, content, created_at, sent_at, turn_id, queue_order
                ) values (?, ?, 'user', ?, ?, ?, ?, 1)
                """,
                (turn_id, session_id, user_content, created_at, created_at, turn_id),
            )
            conn.execute(
                """
                insert into session_messages (
                  id, session_id, role, content, created_at, sent_at, turn_id
                ) values (?, ?, 'assistant', ?, ?, ?, ?)
                """,
                (message_id, session_id, assistant_content, created_at, created_at, turn_id),
            )
            conn.execute(
                """
                update sessions
                set unread_count = coalesce(unread_count, 0) + 1,
                    updated_at = ?
                where id = ?
                """,
                (created_at, session_id),
            )
            workspace_id = session["workspace_id"]
            _mark_workspace_unread(conn, session_id, workspace_id, created_at)
    except sqlite3.Error as exc:
        return ConductorMirrorResult("failed", event.event_id, message=str(exc))

    return ConductorMirrorResult(
        action,
        event.event_id,
        session_id=session_id,
        message_id=turn_id,
        message=success_message,
    )


def render_conductor_message_turn(event: InboxItem) -> tuple[str, str]:
    user_text = _render_notification_prompt(
        event,
        marker_label="Event id",
        marker_value=event.event_id,
        command="",
    )
    assistant_text = "\n".join(
        [
            "PR Watch update (synthetic, not user input)",
            f"Event: {event.summary}",
            f"Repo: {event.repo_owner}/{event.repo_name}#{event.pr_number}",
            f"Role: {event.role}",
            f"Type: {event.event_type}",
            f"Actor: {event.actor}",
            f"GitHub: {event.pr_url}",
            f"pr-watch:event_id={event.event_id}",
            "This was inserted by the experimental local Conductor host bridge.",
            "No action has been taken.",
        ]
    )
    return user_text, assistant_text


def render_conductor_message(event: InboxItem, session_id: str = "", message_id: str = "") -> str:
    _user_text, assistant_text = render_conductor_message_turn(event)
    return _render_assistant_payload(event, assistant_text, session_id=session_id, message_id=message_id)


def render_conductor_confirmation_turn(event: InboxItem) -> tuple[str, str]:
    user_text = _render_notification_prompt(
        event,
        marker_label="Confirmation event id",
        marker_value=event.event_id,
        command=f"pr-watch confirm-binding {event.event_id}",
    )
    assistant_text = "\n".join(
        [
            "PR Watch binding confirmation needed (synthetic, not user input)",
            f"Confirm this session should handle PR #{event.pr_number}.",
            f"Event: {event.summary}",
            f"Repo: {event.repo_owner}/{event.repo_name}#{event.pr_number}",
            f"Role: {event.role}",
            f"Type: {event.event_type}",
            f"GitHub: {event.pr_url}",
            f"pr-watch:confirm_event_id={event.event_id}",
            "Use the pr-watch MCP tool confirm_binding_for_event for this event if this is the right session.",
            "No PR inspection, GitHub action, file edit, comment, or push has been performed.",
        ]
    )
    return user_text, assistant_text


def render_conductor_confirmation_message(event: InboxItem, session_id: str = "", message_id: str = "") -> str:
    _user_text, assistant_text = render_conductor_confirmation_turn(event)
    return _render_assistant_payload(
        event,
        assistant_text,
        session_id=session_id,
        message_id=message_id,
        marker={"confirm_event_id": event.event_id},
    )


def _render_notification_prompt(
    event: InboxItem,
    marker_label: str,
    marker_value: str,
    command: str,
) -> str:
    lines = [
        "PR Watch notification only.",
        "",
        f"PR #{event.pr_number}: {event.repo_owner}/{event.repo_name}",
        f"Event: {event.summary}",
        f"Actor: {event.actor}",
        f"Role: {event.role}",
        f"Link: {event.pr_url}",
        f"{marker_label}: {marker_value}",
    ]
    if command:
        lines.append(f"Confirm command: {command}")
        lines.append(f"pr-watch:confirm_event_id={marker_value}")
    else:
        lines.append(f"pr-watch:event_id={marker_value}")
    lines.extend(
        [
            "",
            "Do not inspect files/edit/comment unless user asks.",
            "Do not run tools, call GitHub, edit code, post comments, push, or take external action yet.",
            "Do not treat this as approval to work on the PR.",
        ]
    )
    return "\n".join(lines)


def _render_assistant_payload(
    event: InboxItem,
    text: str,
    session_id: str = "",
    message_id: str = "",
    marker: Optional[dict[str, str]] = None,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "session_id": session_id,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        },
        ensure_ascii=False,
    )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _missing_schema(conn: sqlite3.Connection) -> list[str]:
    missing: list[str] = []
    for table, required_columns in REQUIRED_COLUMNS.items():
        columns = _table_columns(conn, table)
        if not columns:
            missing.append(table)
            continue
        for column in sorted(required_columns - columns):
            missing.append(f"{table}.{column}")
    return missing


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"pragma table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _find_session(conn: sqlite3.Connection, binding_session_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        select id, workspace_id
        from sessions
        where id = ? or claude_session_id = ?
        order by case when id = ? then 0 else 1 end
        limit 1
        """,
        (binding_session_id, binding_session_id, binding_session_id),
    ).fetchone()


def _find_existing_visible_message_id(conn: sqlite3.Connection, session_id: str, marker: str) -> str:
    row = conn.execute(
        """
        select m.id
        from session_messages m
        left join session_messages u
          on u.session_id = m.session_id
         and u.role = 'user'
         and u.id = m.turn_id
        where m.session_id = ?
          and m.content like ?
          and (
            (m.role = 'user' and m.turn_id = m.id)
            or (
              m.role = 'assistant'
              and m.turn_id is not null
              and m.turn_id != ''
              and u.id is not null
            )
          )
        limit 1
        """,
        (session_id, f"%{marker}%"),
    ).fetchone()
    return str(row["id"]) if row else ""


def _mark_workspace_unread(
    conn: sqlite3.Connection,
    session_id: str,
    workspace_id: Optional[str],
    updated_at: str,
) -> None:
    if workspace_id:
        cursor = conn.execute(
            "update workspaces set unread = 1, updated_at = ? where id = ?",
            (updated_at, workspace_id),
        )
        if cursor.rowcount:
            return
    conn.execute(
        "update workspaces set unread = 1, updated_at = ? where active_session_id = ?",
        (updated_at, session_id),
    )
