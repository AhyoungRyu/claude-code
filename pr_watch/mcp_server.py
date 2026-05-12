from __future__ import annotations

import shutil
import subprocess
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional

from .config import config_bool, load_config, state_db_path
from .delivery import approve_event
from .github import current_user, poll_once as github_poll_once
from .sessions import discover_sessions
from .state import StateStore
from .workflow import create_explicit_binding


_DEFAULT_STATE_DIR: Optional[str] = None


def poll_once(
    repo: Optional[str] = None,
    fixture: Optional[str] = None,
    user: Optional[str] = None,
    state_dir: Optional[str] = None,
    include_drafts: Optional[bool] = None,
) -> Dict[str, Any]:
    """Poll GitHub once and record actionable PR events."""
    state_dir = _effective_state_dir(state_dir)
    config = load_config(state_dir)
    should_include_drafts = (
        include_drafts if include_drafts is not None else config_bool(config, "include_drafts", default=False)
    )
    store = StateStore(state_db_path(state_dir))
    items = github_poll_once(
        store,
        user or current_user(),
        repo=repo,
        fixture=fixture,
        sessions=discover_sessions(),
        include_drafts=should_include_drafts,
    )
    return {
        "count": len(items),
        "include_drafts": should_include_drafts,
        "events": [_to_json(item) for item in items],
    }


def list_inbox(state_dir: Optional[str] = None, include_done: bool = False) -> Dict[str, Any]:
    """List pending PR watcher inbox events."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    return {"events": [_to_json(item) for item in store.list_events(include_done=include_done)]}


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


def list_queue(state_dir: Optional[str] = None) -> Dict[str, Any]:
    """List queued resume commands."""
    state_dir = _effective_state_dir(state_dir)
    store = StateStore(state_db_path(state_dir))
    return {"queue": [_to_json(item) for item in store.list_queue()]}


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
        "executables": {name: shutil.which(name) for name in ["gh", "claude", "codex"]},
        "gh_auth": gh_auth,
        "conductor": "optional, not required",
    }


def build_server() -> Any:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("PR Watch")
    server.tool()(poll_once)
    server.tool()(list_inbox)
    server.tool()(bind_pr)
    server.tool()(approve)
    server.tool()(list_queue)
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
