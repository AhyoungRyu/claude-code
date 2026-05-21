from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import shlex
import uuid
from typing import Any, Dict, Optional


PR_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)"
)
REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_local_time(value: str) -> str:
    if not value:
        return ""
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = _dt.datetime.fromisoformat(normalized)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    local = parsed.astimezone()
    return local.strftime("%Y-%m-%d %H:%M %Z").strip()


def summarize_pr_event(actor: str, summary: str, repo_owner: str, repo_name: str, pr_number: int) -> str:
    repo_ref = f"{repo_owner}/{repo_name}#{pr_number}"
    body = _summary_with_repo_ref(summary, repo_ref, pr_number)
    actor = str(actor or "").strip()
    if actor and not body.lower().startswith(actor.lower()):
        return f"{actor}: {body}"
    return body


def _summary_with_repo_ref(summary: str, repo_ref: str, pr_number: int) -> str:
    body = str(summary or "").strip()
    replacement = f"PR in {repo_ref}"
    pattern = re.compile(rf"\bPR\s*#?{re.escape(str(pr_number))}\b", re.IGNORECASE)
    updated, count = pattern.subn(replacement, body, count=1)
    if count:
        return updated
    return f"{body} in {repo_ref}" if body else repo_ref


def stable_id(*parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def random_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def loads_dict(data: Optional[str]) -> Dict[str, Any]:
    if not data:
        return {}
    value = json.loads(data)
    return value if isinstance(value, dict) else {}


def shell_join(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def parse_pr_url(value: str) -> Optional[Dict[str, object]]:
    match = PR_URL_RE.search(value)
    if not match:
        return None
    return {
        "owner": match.group("owner"),
        "repo": match.group("repo"),
        "number": int(match.group("number")),
        "url": match.group(0),
    }


def normalize_repo_full_name(value: str) -> str:
    repo = " ".join(str(value or "").strip().split()).lower()
    if not REPO_FULL_NAME_RE.match(repo):
        raise ValueError("repository must be in owner/repo form")
    return repo


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)
