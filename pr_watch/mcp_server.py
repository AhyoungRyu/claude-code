from __future__ import annotations

import shutil
import subprocess
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional

from .config import config_bool, load_config, state_db_path
from .delivery import approve_event, notify_prompt_event
from .github import current_user, poll_once as github_poll_once
from .host_adapter import status as host_bridge_status
from .host_adapter import sync_once as host_bridge_sync_once
from .notifications import notify_event, resolve_notification_mode
from .sessions import discover_sessions
from .state import StateStore
from .workflow import (
    confirm_binding_and_mark_handled as workflow_confirm_binding_and_mark_handled,
    confirm_binding_for_event as workflow_confirm_binding_for_event,
    create_explicit_binding,
    dismiss_event as workflow_dismiss_event,
    reject_binding_for_event as workflow_reject_binding_for_event,
)


_DEFAULT_STATE_DIR: Optional[str] = None


def poll_once(
    repo: Optional[str] = None,
    fixture: Optional[str] = None,
    user: Optional[str] = None,
    state_dir: Optional[str] = None,
    include_drafts: Optional[bool] = None,
    notification_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Poll GitHub once and record actionable PR events."""
    state_dir = _effective_state_dir(state_dir)
    config = load_config(state_dir)
    should_include_drafts = (
        include_drafts if include_drafts is not None else config_bool(config, "include_drafts", default=False)
    )
    selected_notification_mode = notification_mode or config.get("notification_mode", "none")
    store = StateStore(state_db_path(state_dir))
    items = github_poll_once(
        store,
        user or current_user(),
        repo=repo,
        fixture=fixture,
        sessions=discover_sessions(),
        include_drafts=should_include_drafts,
        notification_mode=selected_notification_mode,
        notification_host="mcp",
    )
    item_ids = {item.event_id for item in items}
    return {
        "count": len(items),
        "include_drafts": should_include_drafts,
        "notification_mode": selected_notification_mode,
        "resolved_notification_mode": resolve_notification_mode(selected_notification_mode, host="mcp"),
        "events": [_to_json(item) for item in items],
        "notifications": [
            _to_json(item)
            for item in store.list_notifications(include_done=True)
            if item.event_id in item_ids
        ],
    }


def list_inbox(state_dir: Optional[str] = None, include_done: bool = False) -> Dict[str, Any]:
    """List pending PR watcher inbox events."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    return {"events": [_to_json(item) for item in store.list_events(include_done=include_done)]}


def show_pending_pr_actions(state_dir: Optional[str] = None, include_done: bool = False) -> Dict[str, Any]:
    """List PR watcher actions waiting for a user decision."""
    return list_inbox(state_dir=state_dir, include_done=include_done)


def bind_pr(
    pr: str,
    role: str,
    agent: str,
    session_id: str,
    cwd: str = "",
    branch: str = "",
    host: Optional[str] = None,
    repo: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Bind a PR to a Claude or Codex session."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    binding = create_explicit_binding(
        store,
        pr,
        role=role,
        agent=agent,
        session_id=session_id,
        cwd=cwd,
        branch=branch,
        host=host,
        repo=repo,
    )
    return {"binding": _to_json(binding)}


def confirm_binding_for_event(
    event_id: str,
    session_id: Optional[str] = None,
    mirror_now: bool = True,
    trigger: bool = False,
    host: str = "conductor",
    conductor_db_path: Optional[str] = None,
    session_state: str = "unknown",
    busy_policy: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Confirm or reassign the active PR/session binding without approving delivery."""
    state_dir = _effective_state_dir(state_dir)
    config = load_config(state_dir)
    store = StateStore(state_db_path(state_dir))
    result = workflow_confirm_binding_for_event(
        store,
        event_id,
        session_id=session_id,
        mirror_now=mirror_now,
        trigger=trigger,
        host=host,
        conductor_db_path=conductor_db_path,
        session_state=session_state,
        busy_policy=busy_policy or config.get("busy_policy", "run_if_idle_queue_if_busy"),
    )
    return _to_json(result)


def confirm_binding_and_mark_handled(
    event_id: str,
    session_id: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Confirm the PR/session binding and dismiss the current event as already handled."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    result = workflow_confirm_binding_and_mark_handled(
        store,
        event_id,
        session_id=session_id,
    )
    return _to_json(result)


def reject_binding_for_event(
    event_id: str,
    session_id: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Reject an inferred PR/session binding candidate without dismissing the event."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    result = workflow_reject_binding_for_event(store, event_id, session_id=session_id)
    return _to_json(result)


def dismiss_event(event_id: str, state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Dismiss a PR watcher event without taking PR or GitHub action."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    result = workflow_dismiss_event(store, event_id)
    return _to_json(result)


def approve(
    event_id: str,
    session_state: str = "unknown",
    busy_policy: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Approve delivery for an inbox event."""
    state_dir = _effective_state_dir(state_dir)
    config = load_config(state_dir)
    store = StateStore(state_db_path(state_dir))
    result = approve_event(
        store,
        event_id,
        session_state=session_state,
        busy_policy=busy_policy or config.get("busy_policy", "run_if_idle_queue_if_busy"),
    )
    return _to_json(result)


def approve_resume_session(
    event_id: str,
    session_state: str = "unknown",
    busy_policy: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Approve resume delivery for a PR watcher event."""
    return approve(
        event_id=event_id,
        session_state=session_state,
        busy_policy=busy_policy,
        state_dir=state_dir,
    )


def queue_resume_session(event_id: str, state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Queue resume delivery for a PR watcher event."""
    return approve(
        event_id=event_id,
        session_state="working",
        busy_policy="always_queue",
        state_dir=state_dir,
    )


def notify_prompt_session(
    event_id: str,
    session_state: str = "unknown",
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a safe notification-only prompt into the bound session."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    result = notify_prompt_event(
        store,
        event_id,
        session_state=session_state,
    )
    return _to_json(result)


def notify(
    event_id: str,
    mode: Optional[str] = None,
    force: bool = False,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an independent notification for an inbox event."""
    state_dir = _effective_state_dir(state_dir)
    config = load_config(state_dir)
    store = StateStore(state_db_path(state_dir))
    result = notify_event(
        store,
        event_id,
        mode=mode or config.get("notification_mode", "desktop"),
        force=force,
        host="mcp",
    )
    return _to_json(result)


def list_queue(state_dir: Optional[str] = None) -> Dict[str, Any]:
    """List queued resume commands."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    return {"queue": [_to_json(item) for item in store.list_queue()]}


def list_notifications(state_dir: Optional[str] = None, include_done: bool = False) -> Dict[str, Any]:
    """List pending in-app notifications and failed notification attempts."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    return {"notifications": [_to_json(item) for item in store.list_notifications(include_done=include_done)]}


def show_in_app_notifications(state_dir: Optional[str] = None, include_done: bool = False) -> Dict[str, Any]:
    """List notifications intended for an app-hosted inbox."""
    return list_notifications(state_dir=state_dir, include_done=include_done)


def ack_notification(notification_id: str, state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Mark an in-app notification as read/done."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    notification = store.update_notification_status(notification_id, "done")
    return {"action": "acked", "notification": _to_json(notification)}


def check_pr_updates(
    repo: Optional[str] = None,
    fixture: Optional[str] = None,
    user: Optional[str] = None,
    state_dir: Optional[str] = None,
    include_drafts: Optional[bool] = None,
    notification_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """User-friendly alias for polling PR updates once."""
    return poll_once(
        repo=repo,
        fixture=fixture,
        user=user,
        state_dir=state_dir,
        include_drafts=include_drafts,
        notification_mode=notification_mode,
    )


def host_status(conductor_db_path: Optional[str] = None) -> Dict[str, Any]:
    """Report host bridge support for Conductor and Codex App."""
    return _to_json(host_bridge_status(conductor_db_path=conductor_db_path))


def sync_host_once(
    host: str = "all",
    conductor_db_path: Optional[str] = None,
    trigger_confirmed: bool = False,
    notify_prompt_confirmed: bool = False,
    session_state: str = "unknown",
    busy_policy: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Sync pending events to local host sessions/surfaces and optionally trigger confirmed bindings."""
    state_dir = _effective_state_dir(state_dir)
    config = load_config(state_dir)
    store = StateStore(state_db_path(state_dir))
    result = host_bridge_sync_once(
        store,
        hosts=[host],
        conductor_db_path=conductor_db_path,
        trigger_confirmed=trigger_confirmed,
        notify_prompt_confirmed=notify_prompt_confirmed,
        session_state=session_state,
        busy_policy=busy_policy or config.get("busy_policy", "run_if_idle_queue_if_busy"),
    )
    return _to_json(result)


def doctor(state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Return local dependency and configuration diagnostics."""
    state_dir = _effective_state_dir(state_dir)
    config = load_config(state_dir)
    gh_auth = None
    if shutil.which("gh"):
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
        gh_auth = result.returncode == 0
    return {
        "state": str(state_db_path(state_dir)),
        "busy_policy": config.get("busy_policy"),
        "include_drafts": config.get("include_drafts"),
        "notification_mode": config.get("notification_mode"),
        "executables": {name: shutil.which(name) for name in ["gh", "claude", "codex", "osascript", "terminal-notifier"]},
        "gh_auth": gh_auth,
        "conductor": "optional, not required",
    }


def build_server() -> Any:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("PR Watch")
    server.tool()(poll_once)
    server.tool()(check_pr_updates)
    server.tool()(list_inbox)
    server.tool()(show_pending_pr_actions)
    server.tool()(bind_pr)
    server.tool()(confirm_binding_for_event)
    server.tool()(confirm_binding_and_mark_handled)
    server.tool()(reject_binding_for_event)
    server.tool()(dismiss_event)
    server.tool()(approve)
    server.tool()(approve_resume_session)
    server.tool()(queue_resume_session)
    server.tool()(notify_prompt_session)
    server.tool()(notify)
    server.tool()(list_queue)
    server.tool()(list_notifications)
    server.tool()(show_in_app_notifications)
    server.tool()(ack_notification)
    server.tool()(host_status)
    server.tool()(sync_host_once)
    server.tool()(doctor)
    return server


def run_server(state_dir: Optional[str] = None) -> None:
    global _DEFAULT_STATE_DIR
    _DEFAULT_STATE_DIR = state_dir
    build_server().run(transport="stdio")


def _effective_state_dir(state_dir: Optional[str]) -> Optional[str]:
    return state_dir or _DEFAULT_STATE_DIR


def _to_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_json(item) for key, item in value.items()}
    return value
