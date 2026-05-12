from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import SessionInfo
from .util import json_text


BRANCH_RE = re.compile(r"(?:branch|on branch)[:= ]+([A-Za-z0-9._/\-]+)", re.IGNORECASE)


def discover_sessions(home: Optional[Path] = None) -> List[SessionInfo]:
    root = Path(home).expanduser() if home else Path.home()
    sessions: List[SessionInfo] = []
    sessions.extend(_discover_claude(root))
    sessions.extend(_discover_codex(root))
    return sessions


def _discover_claude(home: Path) -> List[SessionInfo]:
    project_root = home / ".claude" / "projects"
    if not project_root.exists():
        return []
    sessions: List[SessionInfo] = []
    for path in project_root.rglob("*.jsonl"):
        records = list(_read_jsonl(path))
        if not records:
            continue
        text = "\n".join(json_text(record) for record in records[-50:])
        latest = records[-1]
        session_id = _first_value(records, ["sessionId", "session_id", "id"]) or path.stem
        cwd = _first_value(records, ["cwd", "currentWorkingDirectory"]) or ""
        title = _first_value(records, ["title", "summary"]) or _text_preview(text)
        branch = _first_value(records, ["branch", "gitBranch"]) or _extract_branch(text)
        last_activity = str(latest.get("timestamp") or latest.get("updated_at") or latest.get("created_at") or "")
        sessions.append(
            SessionInfo(
                agent="claude",
                session_id=session_id,
                title=title,
                cwd=cwd,
                branch=branch,
                text=text,
                host=None,
                last_activity_at=last_activity,
            )
        )
    return sessions


def _discover_codex(home: Path) -> List[SessionInfo]:
    codex_root = home / ".codex"
    candidates = [codex_root / "session_index.jsonl"]
    archive = codex_root / "archived_sessions"
    if archive.exists():
        candidates.extend(archive.glob("*.jsonl"))

    sessions: List[SessionInfo] = []
    for path in candidates:
        if not path.exists():
            continue
        for record in _read_jsonl(path):
            session_id = str(record.get("id") or record.get("session_id") or record.get("sessionId") or "")
            if not session_id:
                continue
            text = json_text(record)
            sessions.append(
                SessionInfo(
                    agent="codex",
                    session_id=session_id,
                    title=str(record.get("title") or record.get("name") or _text_preview(text)),
                    cwd=str(record.get("cwd") or record.get("workspace") or ""),
                    branch=str(record.get("branch") or record.get("git_branch") or _extract_branch(text)),
                    text=text,
                    host=record.get("host"),
                    last_activity_at=str(record.get("updated_at") or record.get("last_activity_at") or ""),
                )
            )
    sessions.extend(_discover_codex_rollouts(codex_root))
    return sessions


def _discover_codex_rollouts(codex_root: Path) -> List[SessionInfo]:
    sessions_root = codex_root / "sessions"
    if not sessions_root.exists():
        return []
    sessions: List[SessionInfo] = []
    for path in sessions_root.rglob("*.jsonl"):
        records = list(_read_jsonl(path))
        if not records:
            continue
        meta = _codex_session_meta(records)
        session_id = str(meta.get("id") or path.stem)
        cwd = str(meta.get("cwd") or "")
        text = "\n".join(_codex_record_text(record) for record in records if _codex_record_text(record))
        sessions.append(
            SessionInfo(
                agent="codex",
                session_id=session_id,
                title=_text_preview(text) or str(meta.get("source") or path.stem),
                cwd=cwd,
                branch=_extract_branch(text),
                text=text,
                host="codex_cli",
                last_activity_at=str(meta.get("timestamp") or ""),
            )
        )
    return sessions


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def _codex_session_meta(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    for record in records:
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
            return record["payload"]
    return {}


def _codex_record_text(record: Dict[str, Any]) -> str:
    payload = record.get("payload")
    if isinstance(payload, dict):
        for key in ("message", "last_agent_message"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        content = payload.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "\n".join(parts)
    return ""


def _first_value(records: List[Dict[str, Any]], keys: List[str]) -> str:
    for record in reversed(records):
        for key in keys:
            value = record.get(key)
            if value:
                return str(value)
    return ""


def _extract_branch(text: str) -> str:
    match = BRANCH_RE.search(text)
    return match.group(1) if match else ""


def _text_preview(text: str) -> str:
    return " ".join(text.split())[:120]
