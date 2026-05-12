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


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)
