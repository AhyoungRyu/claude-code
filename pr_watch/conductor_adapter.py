from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import Binding, InboxItem
from .util import utc_now


DEFAULT_CONDUCTOR_DB_PATH = (
    Path.home() / "Library" / "Application Support" / "com.conductor.app" / "conductor.db"
)

REQUIRED_COLUMNS = {
    "sessions": {"id", "claude_session_id", "unread_count", "updated_at", "workspace_id"},
    "workspaces": {"id", "active_session_id", "unread", "updated_at"},
    "session_messages": {"id", "session_id", "role", "content", "created_at"},
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
            existing_message_id = _find_existing_message_id(conn, session_id, event.event_id)
            if existing_message_id:
                return ConductorMirrorResult(
                    "already_synced",
                    event.event_id,
                    session_id=session_id,
                    message_id=existing_message_id,
                    message="event already mirrored into Conductor",
                )

            message_id = str(uuid.uuid4())
            created_at = utc_now()
            content = render_conductor_message(event, session_id=session_id, message_id=message_id)
            conn.execute(
                """
                insert into session_messages (
                  id, session_id, role, content, created_at
                ) values (?, ?, 'assistant', ?, ?)
                """,
                (message_id, session_id, content, created_at),
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
        "mirrored",
        event.event_id,
        session_id=session_id,
        message_id=message_id,
        message="event mirrored into Conductor as an assistant-role synthetic message",
    )


def render_conductor_message(event: InboxItem, session_id: str = "", message_id: str = "") -> str:
    text = "\n".join(
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
        ]
    )
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": message_id or f"pr-watch-{event.event_id}",
                "container": None,
                "model": "<synthetic>",
                "role": "assistant",
                "stop_details": None,
                "stop_reason": "stop_sequence",
                "stop_sequence": "",
                "type": "message",
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
                    "service_tier": None,
                },
                "content": [{"type": "text", "text": text}],
            },
            "parent_tool_use_id": None,
            "session_id": session_id,
            "uuid": message_id or event.event_id,
            "pr_watch": {"event_id": event.event_id},
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


def _find_existing_message_id(conn: sqlite3.Connection, session_id: str, event_id: str) -> str:
    marker = f"%pr-watch:event_id={event_id}%"
    row = conn.execute(
        """
        select id from session_messages
        where session_id = ? and content like ?
        limit 1
        """,
        (session_id, marker),
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
